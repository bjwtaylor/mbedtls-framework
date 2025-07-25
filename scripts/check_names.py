#!/usr/bin/env python3
#
# Copyright The Mbed TLS Contributors
# SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later

"""
This script confirms that the naming of all symbols and identifiers in Mbed TLS
are consistent with the house style and are also self-consistent. It only runs
on Linux and macOS since it depends on nm.

It contains three major Python classes, TFPSACryptoCodeParser,
MBEDTLSCodeParser and NameChecker. They all have a comprehensive "run-all"
function (comprehensive_parse() and perform_checks()) but the individual
functions can also be used for specific needs.

CodeParser(a inherent base class for TFPSACryptoCodeParser and MBEDTLSCodeParser)
makes heavy use of regular expressions to parse the code, and is dependent on
the current code formatting. Many Python C parser libraries require
preprocessed C code, which means no macro parsing. Compiler tools are also not
very helpful when we want the exact location in the original source (which
becomes impossible when e.g. comments are stripped).

NameChecker performs the following checks:

- All exported and available symbols in the library object files, are explicitly
  declared in the header files. This uses the nm command.
- All macros, constants, and identifiers (function names, struct names, etc)
  follow the required regex pattern.
- Typo checking: All words that begin with MBED|PSA exist as macros or constants.

The script returns 0 on success, 1 on test failure, and 2 if there is a script
error. It must be run from Mbed TLS root.
"""

import abc
import argparse
import fnmatch
import glob
import textwrap
import os
import sys
import traceback
import re
import enum
import shutil
import subprocess
import logging
import tempfile

import project_scripts # pylint: disable=unused-import
from mbedtls_framework import build_tree


# Naming patterns to check against. These are defined outside the NameCheck
# class for ease of modification.
PUBLIC_MACRO_PATTERN = r"^(MBEDTLS|PSA|TF_PSA_CRYPTO)_[0-9A-Z_]*[0-9A-Z]$"
INTERNAL_MACRO_PATTERN = r"^[0-9A-Za-z_]*[0-9A-Z]$"
CONSTANTS_PATTERN = PUBLIC_MACRO_PATTERN
IDENTIFIER_PATTERN = r"^(mbedtls|psa|tf_psa_crypto)_[0-9a-z_]*[0-9a-z]$"

class Match(): # pylint: disable=too-few-public-methods
    """
    A class representing a match, together with its found position.

    Fields:
    * filename: the file that the match was in.
    * line: the full line containing the match.
    * line_no: the line number.
    * pos: a tuple of (start, end) positions on the line where the match is.
    * name: the match itself.
    """
    def __init__(self, filename, line, line_no, pos, name):
        # pylint: disable=too-many-arguments
        self.filename = filename
        self.line = line
        self.line_no = line_no
        self.pos = pos
        self.name = name

    def __str__(self):
        """
        Return a formatted code listing representation of the erroneous line.
        """
        gutter = format(self.line_no, "4d")
        underline = self.pos[0] * " " + (self.pos[1] - self.pos[0]) * "^"

        return (
            " {0} |\n".format(" " * len(gutter)) +
            " {0} | {1}".format(gutter, self.line) +
            " {0} | {1}\n".format(" " * len(gutter), underline)
        )

class Problem(abc.ABC): # pylint: disable=too-few-public-methods
    """
    An abstract parent class representing a form of static analysis error.
    It extends an Abstract Base Class, which means it is not instantiable, and
    it also mandates certain abstract methods to be implemented in subclasses.
    """
    # Class variable to control the quietness of all problems
    quiet = False
    def __init__(self):
        self.textwrapper = textwrap.TextWrapper()
        self.textwrapper.width = 80
        self.textwrapper.initial_indent = "    > "
        self.textwrapper.subsequent_indent = "      "

    def __str__(self):
        """
        Unified string representation method for all Problems.
        """
        if self.__class__.quiet:
            return self.quiet_output()
        return self.verbose_output()

    @abc.abstractmethod
    def quiet_output(self):
        """
        The output when --quiet is enabled.
        """
        pass

    @abc.abstractmethod
    def verbose_output(self):
        """
        The default output with explanation and code snippet if appropriate.
        """
        pass

class SymbolNotInHeader(Problem): # pylint: disable=too-few-public-methods
    """
    A problem that occurs when an exported/available symbol in the object file
    is not explicitly declared in header files. Created with
    NameCheck.check_symbols_declared_in_header()

    Fields:
    * symbol_name: the name of the symbol.
    """
    def __init__(self, symbol_name):
        self.symbol_name = symbol_name
        Problem.__init__(self)

    def quiet_output(self):
        return "{0}".format(self.symbol_name)

    def verbose_output(self):
        return self.textwrapper.fill(
            "'{0}' was found as an available symbol in the output of nm, "
            "however it was not declared in any header files."
            .format(self.symbol_name))

class PatternMismatch(Problem): # pylint: disable=too-few-public-methods
    """
    A problem that occurs when something doesn't match the expected pattern.
    Created with NameCheck.check_match_pattern()

    Fields:
    * pattern: the expected regex pattern
    * match: the Match object in question
    """
    def __init__(self, pattern, match):
        self.pattern = pattern
        self.match = match
        Problem.__init__(self)


    def quiet_output(self):
        return (
            "{0}:{1}:{2}"
            .format(self.match.filename, self.match.line_no, self.match.name)
        )

    def verbose_output(self):
        return self.textwrapper.fill(
            "{0}:{1}: '{2}' does not match the required pattern '{3}'."
            .format(
                self.match.filename,
                self.match.line_no,
                self.match.name,
                self.pattern
            )
        ) + "\n" + str(self.match)

class Typo(Problem): # pylint: disable=too-few-public-methods
    """
    A problem that occurs when a word using MBED or PSA doesn't
    appear to be defined as constants nor enum values. Created with
    NameCheck.check_for_typos()

    Fields:
    * match: the Match object of the MBED|PSA name in question.
    """
    def __init__(self, match):
        self.match = match
        Problem.__init__(self)

    def quiet_output(self):
        return (
            "{0}:{1}:{2}"
            .format(self.match.filename, self.match.line_no, self.match.name)
        )

    def verbose_output(self):
        return self.textwrapper.fill(
            "{0}:{1}: '{2}' looks like a typo. It was not found in any "
            "macros or any enums. If this is not a typo, put "
            "//no-check-names after it."
            .format(self.match.filename, self.match.line_no, self.match.name)
        ) + "\n" + str(self.match)

class CodeParser():
    """
    Class for retrieving files and parsing the code. This can be used
    independently of the checks that NameChecker performs, for example for
    list_internal_identifiers.py.
    """
    def __init__(self, log):
        self.log = log
        if not build_tree.looks_like_root(os.getcwd()):
            raise Exception("This script must be run from Mbed TLS or TF-PSA-Crypto root")

        # Memo for storing "glob expression": set(filepaths)
        self.files = {}

        # Globally excluded filenames.
        # Note that "*" can match directory separators in exclude lists.
        self.excluded_files = ["*/bn_mul", "*/compat-2.x.h"]

    def _parse(self, all_macros, enum_consts, identifiers,
               excluded_identifiers, mbed_psa_words, symbols):
        # pylint: disable=too-many-arguments
        """
        Parse macros, enums, identifiers, excluded identifiers, Mbed PSA word and Symbols.

        Returns a dict of parsed item key to the corresponding List of Matches.
        """

        self.log.info("Parsing source code...")
        self.log.debug(
            "The following files are excluded from the search: {}"
            .format(str(self.excluded_files))
        )

        # Remove identifier macros like mbedtls_printf or mbedtls_calloc
        identifiers_justname = [x.name for x in identifiers]
        actual_macros = {"public": [], "internal": []}
        for scope in actual_macros:
            for macro in all_macros[scope]:
                if macro.name not in identifiers_justname:
                    actual_macros[scope].append(macro)

        self.log.debug("Found:")
        # Aligns the counts on the assumption that none exceeds 4 digits
        for scope in actual_macros:
            self.log.debug("  {:4} Total {} Macros"
                           .format(len(all_macros[scope]), scope))
            self.log.debug("  {:4} {} Non-identifier Macros"
                           .format(len(actual_macros[scope]), scope))
        self.log.debug("  {:4} Enum Constants".format(len(enum_consts)))
        self.log.debug("  {:4} Identifiers".format(len(identifiers)))
        self.log.debug("  {:4} Exported Symbols".format(len(symbols)))
        return {
            "public_macros": actual_macros["public"],
            "internal_macros": actual_macros["internal"],
            "private_macros": all_macros["private"],
            "enum_consts": enum_consts,
            "identifiers": identifiers,
            "excluded_identifiers": excluded_identifiers,
            "symbols": symbols,
            "mbed_psa_words": mbed_psa_words
        }

    def is_file_excluded(self, path, exclude_wildcards):
        """Whether the given file path is excluded."""
        # exclude_wildcards may be None. Also, consider the global exclusions.
        exclude_wildcards = (exclude_wildcards or []) + self.excluded_files
        for pattern in exclude_wildcards:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def get_all_files(self, include_wildcards, exclude_wildcards):
        """
        Get all files that match any of the included UNIX-style wildcards
        and filter them into included and excluded lists.
        While the check_names script is designed only for use on UNIX/macOS
        (due to nm), this function alone will work fine on Windows even with
        forward slashes in the wildcard.

        Args:
        * include_wildcards: a List of shell-style wildcards to match filepaths.
        * exclude_wildcards: a List of shell-style wildcards to exclude.

        Returns:
        * inc_files: A List of relative filepaths for included files.
        * exc_files: A List of relative filepaths for excluded files.
        """
        accumulator = set()
        all_wildcards = include_wildcards + (exclude_wildcards or [])
        for wildcard in all_wildcards:
            accumulator = accumulator.union(glob.iglob(wildcard))

        inc_files = []
        exc_files = []
        for path in accumulator:
            if self.is_file_excluded(path, exclude_wildcards):
                exc_files.append(path)
            else:
                inc_files.append(path)
        return (inc_files, exc_files)

    def get_included_files(self, include_wildcards, exclude_wildcards):
        """
        Get all files that match any of the included UNIX-style wildcards.
        While the check_names script is designed only for use on UNIX/macOS
        (due to nm), this function alone will work fine on Windows even with
        forward slashes in the wildcard.

        Args:
        * include_wildcards: a List of shell-style wildcards to match filepaths.
        * exclude_wildcards: a List of shell-style wildcards to exclude.

        Returns a List of relative filepaths.
        """
        accumulator = set()

        for include_wildcard in include_wildcards:
            accumulator = accumulator.union(glob.iglob(include_wildcard))

        return list(path for path in accumulator
                    if not self.is_file_excluded(path, exclude_wildcards))

    def parse_macros(self, include, exclude=None):
        """
        Parse all macros defined by #define preprocessor directives.

        Args:
        * include: A List of glob expressions to look for files through.
        * exclude: A List of glob expressions for excluding files.

        Returns a List of Match objects for the found macros.
        """
        macro_regex = re.compile(r"# *define +(?P<macro>\w+)")
        exclusions = (
            "asm", "inline", "EMIT", "_CRT_SECURE_NO_DEPRECATE", "MULADDC_"
        )

        files = self.get_included_files(include, exclude)
        self.log.debug("Looking for macros in {} files".format(len(files)))

        macros = []
        for header_file in files:
            with open(header_file, "r", encoding="utf-8") as header:
                for line_no, line in enumerate(header):
                    for macro in macro_regex.finditer(line):
                        if macro.group("macro").startswith(exclusions):
                            continue

                        macros.append(Match(
                            header_file,
                            line,
                            line_no,
                            macro.span("macro"),
                            macro.group("macro")))

        return macros

    def parse_mbed_psa_words(self, include, exclude=None):
        """
        Parse all words in the file that begin with MBED|PSA, in and out of
        macros, comments, anything.

        Args:
        * include: A List of glob expressions to look for files through.
        * exclude: A List of glob expressions for excluding files.

        Returns a List of Match objects for words beginning with MBED|PSA.
        """
        # Typos of TLS are common, hence the broader check below than MBEDTLS.
        mbed_regex = re.compile(r"\b(MBED.+?|PSA)_[A-Z0-9_]*")
        exclusions = re.compile(r"// *no-check-names|#error")

        files = self.get_included_files(include, exclude)
        self.log.debug(
            "Looking for MBED|PSA words in {} files"
            .format(len(files))
        )

        mbed_psa_words = []
        for filename in files:
            with open(filename, "r", encoding="utf-8") as fp:
                for line_no, line in enumerate(fp):
                    if exclusions.search(line):
                        continue

                    for name in mbed_regex.finditer(line):
                        mbed_psa_words.append(Match(
                            filename,
                            line,
                            line_no,
                            name.span(0),
                            name.group(0)))

        return mbed_psa_words

    def parse_enum_consts(self, include, exclude=None):
        """
        Parse all enum value constants that are declared.

        Args:
        * include: A List of glob expressions to look for files through.
        * exclude: A List of glob expressions for excluding files.

        Returns a List of Match objects for the findings.
        """
        files = self.get_included_files(include, exclude)
        self.log.debug("Looking for enum consts in {} files".format(len(files)))

        # Emulate a finite state machine to parse enum declarations.
        # OUTSIDE_KEYWORD = outside the enum keyword
        # IN_BRACES = inside enum opening braces
        # IN_BETWEEN = between enum keyword and opening braces
        states = enum.Enum("FSM", ["OUTSIDE_KEYWORD", "IN_BRACES", "IN_BETWEEN"])
        enum_consts = []
        for header_file in files:
            state = states.OUTSIDE_KEYWORD
            with open(header_file, "r", encoding="utf-8") as header:
                for line_no, line in enumerate(header):
                    # Match typedefs and brackets only when they are at the
                    # beginning of the line -- if they are indented, they might
                    # be sub-structures within structs, etc.
                    optional_c_identifier = r"([_a-zA-Z][_a-zA-Z0-9]*)?"
                    if (state == states.OUTSIDE_KEYWORD and
                            re.search(r"^(typedef +)?enum " + \
                                    optional_c_identifier + \
                                    r" *{", line)):
                        state = states.IN_BRACES
                    elif (state == states.OUTSIDE_KEYWORD and
                          re.search(r"^(typedef +)?enum", line)):
                        state = states.IN_BETWEEN
                    elif (state == states.IN_BETWEEN and
                          re.search(r"^{", line)):
                        state = states.IN_BRACES
                    elif (state == states.IN_BRACES and
                          re.search(r"^}", line)):
                        state = states.OUTSIDE_KEYWORD
                    elif (state == states.IN_BRACES and
                          not re.search(r"^ *#", line)):
                        enum_const = re.search(r"^ *(?P<enum_const>\w+)", line)
                        if not enum_const:
                            continue

                        enum_consts.append(Match(
                            header_file,
                            line,
                            line_no,
                            enum_const.span("enum_const"),
                            enum_const.group("enum_const")))

        return enum_consts

    IGNORED_CHUNK_REGEX = re.compile('|'.join([
        r'/\*.*?\*/', # block comment entirely on one line
        r'//.*', # line comment
        r'(?P<string>")(?:[^\\\"]|\\.)*"', # string literal
    ]))

    def strip_comments_and_literals(self, line, in_block_comment):
        """Strip comments and string literals from line.

        Continuation lines are not supported.

        If in_block_comment is true, assume that the line starts inside a
        block comment.

        Return updated values of (line, in_block_comment) where:
        * Comments in line have been replaced by a space (or nothing at the
          start or end of the line).
        * String contents have been removed.
        * in_block_comment indicates whether the line ends inside a block
          comment that continues on the next line.
        """

        # Terminate current multiline comment?
        if in_block_comment:
            m = re.search(r"\*/", line)
            if m:
                in_block_comment = False
                line = line[m.end(0):]
            else:
                return '', True

        # Remove full comments and string literals.
        # Do it all together to handle cases like "/*" correctly.
        # Note that continuation lines are not supported.
        line = re.sub(self.IGNORED_CHUNK_REGEX,
                      lambda s: '""' if s.group('string') else ' ',
                      line)

        # Start an unfinished comment?
        # (If `/*` was part of a complete comment, it's already been removed.)
        m = re.search(r"/\*", line)
        if m:
            in_block_comment = True
            line = line[:m.start(0)]

        return line, in_block_comment

    IDENTIFIER_REGEX = re.compile('|'.join([
        # Match " something(a" or " *something(a". Functions.
        # Assumptions:
        # - function definition from return type to one of its arguments is
        #   all on one line
        # - function definition line only contains alphanumeric, asterisk,
        #   underscore, and open bracket
        r".* \**(\w+) *\( *\w",
        # Match "(*something)(".
        r".*\( *\* *(\w+) *\) *\(",
        # Match names of named data structures.
        r"(?:typedef +)?(?:struct|union|enum) +(\w+)(?: *{)?$",
        # Match names of typedef instances, after closing bracket.
        r"}? *(\w+)[;[].*",
    ]))
    # The regex below is indented for clarity.
    EXCLUSION_LINES = re.compile("|".join([
        r"extern +\"C\"",
        r"(typedef +)?(struct|union|enum)( *{)?$",
        r"} *;?$",
        r"$",
        r"//",
        r"#",
    ]))

    def parse_identifiers_in_file(self, header_file, identifiers):
        """
        Parse all lines of a header where a function/enum/struct/union/typedef
        identifier is declared, based on some regex and heuristics. Highly
        dependent on formatting style.

        Append found matches to the list ``identifiers``.
        """

        with open(header_file, "r", encoding="utf-8") as header:
            in_block_comment = False
            # The previous line variable is used for concatenating lines
            # when identifiers are formatted and spread across multiple
            # lines.
            previous_line = ""

            for line_no, line in enumerate(header):
                line, in_block_comment = \
                    self.strip_comments_and_literals(line, in_block_comment)

                if self.EXCLUSION_LINES.match(line):
                    previous_line = ""
                    continue

                # If the line contains only space-separated alphanumeric
                # characters (or underscore, asterisk, or open parenthesis),
                # and nothing else, high chance it's a declaration that
                # continues on the next line
                if re.search(r"^([\w\*\(]+\s+)+$", line):
                    previous_line += line
                    continue

                # If previous line seemed to start an unfinished declaration
                # (as above), concat and treat them as one.
                if previous_line:
                    line = previous_line.strip() + " " + line.strip() + "\n"
                    previous_line = ""

                # Skip parsing if line has a space in front = heuristic to
                # skip function argument lines (highly subject to formatting
                # changes)
                if line[0] == " ":
                    continue

                identifier = self.IDENTIFIER_REGEX.search(line)

                if not identifier:
                    continue

                # Find the group that matched, and append it
                for group in identifier.groups():
                    if not group:
                        continue

                    identifiers.append(Match(
                        header_file,
                        line,
                        line_no,
                        identifier.span(),
                        group))

    def parse_identifiers(self, include, exclude=None):
        """
        Parse all lines of a header where a function/enum/struct/union/typedef
        identifier is declared, based on some regex and heuristics. Highly
        dependent on formatting style. Identifiers in excluded files are still
        parsed

        Args:
        * include: A List of glob expressions to look for files through.
        * exclude: A List of glob expressions for excluding files.

        Returns: a Tuple of two Lists of Match objects with identifiers.
        * included_identifiers: A List of Match objects with identifiers from
          included files.
        * excluded_identifiers: A List of Match objects with identifiers from
          excluded files.
        """

        included_files, excluded_files = \
            self.get_all_files(include, exclude)

        self.log.debug("Looking for included identifiers in {} files".format \
            (len(included_files)))

        included_identifiers = []
        excluded_identifiers = []
        for header_file in included_files:
            self.parse_identifiers_in_file(header_file, included_identifiers)
        for header_file in excluded_files:
            self.parse_identifiers_in_file(header_file, excluded_identifiers)

        return (included_identifiers, excluded_identifiers)

    def parse_symbols(self):
        """
        Compile a library, and parse the object files using nm to retrieve the
        list of referenced symbols. Exceptions thrown here are rethrown because
        they would be critical errors that void several tests, and thus needs
        to halt the program. This is explicitly done for clarity.

        Returns a List of unique symbols defined and used in the libraries.
        """
        raise NotImplementedError("parse_symbols must be implemented by a code parser")

    def comprehensive_parse(self):
        """
        (Must be defined as a class method)
        Comprehensive ("default") function to call each parsing function and
        retrieve various elements of the code, together with the source location.

        Returns a dict of parsed item key to the corresponding List of Matches.
        """
        raise NotImplementedError("comprehension_parse must be implemented by a code parser")

    def parse_symbols_from_nm(self, object_files):
        """
        Run nm to retrieve the list of referenced symbols in each object file.
        Does not return the position data since it is of no use.

        Args:
        * object_files: a List of compiled object filepaths to search through.

        Returns a List of unique symbols defined and used in any of the object
        files.
        """
        nm_undefined_regex = re.compile(r"^\S+: +U |^$|^\S+:$")
        nm_valid_regex = re.compile(r"^\S+( [0-9A-Fa-f]+)* . _*(?P<symbol>\w+)")
        exclusions = ("FStar", "Hacl")
        symbols = []
        # Gather all outputs of nm
        nm_output = ""
        for lib in object_files:
            nm_output += subprocess.run(
                ["nm", "-og", lib],
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True
            ).stdout
        for line in nm_output.splitlines():
            if not nm_undefined_regex.search(line):
                symbol = nm_valid_regex.search(line)
                if (symbol and not symbol.group("symbol").startswith(exclusions)):
                    symbols.append(symbol.group("symbol"))
                else:
                    self.log.error(line)
        return symbols

class TFPSACryptoCodeParser(CodeParser):
    """
    Class for retrieving files and parsing TF-PSA-Crypto code. This can be used
    independently of the checks that NameChecker performs.
    """

    def __init__(self, log):
        super().__init__(log)
        if not build_tree.looks_like_tf_psa_crypto_root(os.getcwd()):
            raise Exception("This script must be run from TF-PSA-Crypto root.")

    def comprehensive_parse(self):
        """
        Comprehensive ("default") function to call each parsing function and
        retrieve various elements of the code, together with the source location.

        Returns a dict of parsed item key to the corresponding List of Matches.
        """
        all_macros = {"public": [], "internal": [], "private":[]}
        all_macros["public"] = self.parse_macros([
            "include/psa/*.h",
            "include/tf-psa-crypto/*.h",
            "include/mbedtls/*.h",
            "drivers/builtin/include/mbedtls/*.h",
            "drivers/everest/include/everest/everest.h",
            "drivers/everest/include/everest/x25519.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/everest.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/x25519.h"
        ])
        all_macros["internal"] = self.parse_macros([
            "core/*.h",
            "drivers/builtin/src/*.h",
            "framework/tests/include/test/drivers/*.h",
        ])
        all_macros["private"] = self.parse_macros([
            "core/*.c",
            "drivers/builtin/src/*.c",
        ])
        enum_consts = self.parse_enum_consts([
            "include/psa/*.h",
            "include/tf-psa-crypto/*.h",
            "include/mbedtls/*.h",
            "drivers/builtin/include/mbedtls/*.h",
            "core/*.h",
            "drivers/builtin/src/*.h",
            "core/*.c",
            "drivers/builtin/src/*.c",
            "drivers/everest/include/everest/everest.h",
            "drivers/everest/include/everest/x25519.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/everest.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/x25519.h"
        ])
        identifiers, excluded_identifiers = self.parse_identifiers([
            "include/psa/*.h",
            "include/tf-psa-crypto/*.h",
            "include/mbedtls/*.h",
            "drivers/builtin/include/mbedtls/*.h",
            "core/*.h",
            "drivers/builtin/src/*.h",
            "drivers/everest/include/everest/everest.h",
            "drivers/everest/include/everest/x25519.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/everest.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/x25519.h"
        ], ["drivers/p256-m/p256-m/p256-m.h"])
        mbed_psa_words = self.parse_mbed_psa_words([
            "include/psa/*.h",
            "include/tf-psa-crypto/*.h",
            "include/mbedtls/*.h",
            "drivers/builtin/include/mbedtls/*.h",
            "core/*.h",
            "drivers/builtin/src/*.h",
            "drivers/everest/include/everest/everest.h",
            "drivers/everest/include/everest/x25519.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/everest.h",
            "drivers/everest/include/tf-psa-crypto/private/everest/x25519.h",
            "core/*.c",
            "drivers/builtin/src/*.c",
            "drivers/everest/library/everest.c",
            "drivers/everest/library/x25519.c"
        ], ["core/psa_crypto_driver_wrappers.h"])
        symbols = self.parse_symbols()

        return self._parse(all_macros, enum_consts, identifiers,
                           excluded_identifiers, mbed_psa_words, symbols)

    def parse_symbols(self):
        """
        Compile the TF-PSA-Crypto libraries, and parse the
        object files using nm to retrieve the list of referenced symbols.
        Exceptions thrown here are rethrown because they would be critical
        errors that void several tests, and thus needs to halt the program. This
        is explicitly done for clarity.

        Returns a List of unique symbols defined and used in the libraries.
        """
        self.log.info("Compiling...")
        symbols = []

        # Back up the config and atomically compile with the full configuration.
        shutil.copy(
            "include/psa/crypto_config.h",
            "include/psa/crypto_config.h.bak"
        )
        try:
            # Use check=True in all subprocess calls so that failures are raised
            # as exceptions and logged.
            subprocess.run(
                ["python3", "scripts/config.py", "full"],
                universal_newlines=True,
                check=True
            )
            my_environment = os.environ.copy()
            my_environment["CFLAGS"] = "-fno-asynchronous-unwind-tables"

            source_dir = os.getcwd()
            build_dir = tempfile.mkdtemp()
            os.chdir(build_dir)
            subprocess.run(
                ["cmake", "-DGEN_FILES=ON", source_dir],
                universal_newlines=True,
                check=True
            )
            subprocess.run(
                ["make"],
                env=my_environment,
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True
            )

            # Perform object file analysis using nm
            symbols = self.parse_symbols_from_nm([
                build_dir + "/core/libtfpsacrypto.a"
            ])

            os.chdir(source_dir)
            shutil.rmtree(build_dir)
        except subprocess.CalledProcessError as error:
            self.log.debug(error.output)
            raise error
        finally:
            # Put back the original config regardless of there being errors.
            # Works also for keyboard interrupts.
            shutil.move(
                "include/psa/crypto_config.h.bak",
                "include/psa/crypto_config.h"
            )

        return symbols

class MBEDTLSCodeParser(CodeParser):
    """
    Class for retrieving files and parsing Mbed TLS code. This can be used
    independently of the checks that NameChecker performs.
    """

    def __init__(self, log):
        super().__init__(log)
        if not build_tree.looks_like_mbedtls_root(os.getcwd()):
            raise Exception("This script must be run from Mbed TLS root.")

    def comprehensive_parse(self):
        """
        Comprehensive ("default") function to call each parsing function and
        retrieve various elements of the code, together with the source location.

        Returns a dict of parsed item key to the corresponding List of Matches.
        """
        all_macros = {"public": [], "internal": [], "private":[]}
        # TF-PSA-Crypto is in the same repo in 3.6 so initalise variable here.
        tf_psa_crypto_parse_result = {}

        if build_tree.is_mbedtls_3_6():
            all_macros["public"] = self.parse_macros([
                "include/mbedtls/*.h",
                "include/psa/*.h",
                "3rdparty/everest/include/everest/everest.h",
                "3rdparty/everest/include/everest/x25519.h"
            ])
            all_macros["internal"] = self.parse_macros([
                "library/*.h",
                "framework/tests/include/test/drivers/*.h",
            ])
            all_macros["private"] = self.parse_macros([
                "library/*.c",
            ])
            enum_consts = self.parse_enum_consts([
                "include/mbedtls/*.h",
                "include/psa/*.h",
                "library/*.h",
                "library/*.c",
                "3rdparty/everest/include/everest/everest.h",
                "3rdparty/everest/include/everest/x25519.h"
            ])
            identifiers, excluded_identifiers = self.parse_identifiers([
                "include/mbedtls/*.h",
                "include/psa/*.h",
                "library/*.h",
                "3rdparty/everest/include/everest/everest.h",
                "3rdparty/everest/include/everest/x25519.h"
            ], ["3rdparty/p256-m/p256-m/p256-m.h"])
            mbed_psa_words = self.parse_mbed_psa_words([
                "include/mbedtls/*.h",
                "include/psa/*.h",
                "library/*.h",
                "3rdparty/everest/include/everest/everest.h",
                "3rdparty/everest/include/everest/x25519.h",
                "library/*.c",
                "3rdparty/everest/library/everest.c",
                "3rdparty/everest/library/x25519.c"
            ], ["library/psa_crypto_driver_wrappers.h"])
        else:
            all_macros = {"public": [], "internal": [], "private":[]}
            all_macros["public"] = self.parse_macros([
                "include/mbedtls/*.h",
            ])
            all_macros["internal"] = self.parse_macros([
                "library/*.h",
                "framework/tests/include/test/drivers/*.h",
            ])
            all_macros["private"] = self.parse_macros([
                "library/*.c",
            ])
            enum_consts = self.parse_enum_consts([
                "include/mbedtls/*.h",
                "library/*.h",
                "library/*.c",
            ])
            identifiers, excluded_identifiers = self.parse_identifiers([
                "include/mbedtls/*.h",
                "library/*.h",
            ])
            mbed_psa_words = self.parse_mbed_psa_words([
                "include/mbedtls/*.h",
                "library/*.h",
                "library/*.c",
            ])
            os.chdir("./tf-psa-crypto")
            tf_psa_crypto_code_parser = TFPSACryptoCodeParser(self.log)
            tf_psa_crypto_parse_result = tf_psa_crypto_code_parser.comprehensive_parse()
            os.chdir("../")

        symbols = self.parse_symbols()
        mbedtls_parse_result = self._parse(all_macros, enum_consts,
                                           identifiers, excluded_identifiers,
                                           mbed_psa_words, symbols)
        # Combile results for Mbed TLS and TF-PSA-Crypto
        for key in tf_psa_crypto_parse_result:
            mbedtls_parse_result[key] += tf_psa_crypto_parse_result[key]
        return mbedtls_parse_result

    def parse_symbols(self):
        """
        Compile the Mbed TLS libraries, and parse the TLS, Crypto, and x509
        object files using nm to retrieve the list of referenced symbols.
        Exceptions thrown here are rethrown because they would be critical
        errors that void several tests, and thus needs to halt the program. This
        is explicitly done for clarity.

        Returns a List of unique symbols defined and used in the libraries.
        """
        self.log.info("Compiling...")
        symbols = []

        # Back up the config and atomically compile with the full configuration.
        shutil.copy(
            "include/mbedtls/mbedtls_config.h",
            "include/mbedtls/mbedtls_config.h.bak"
        )
        try:
            # Use check=True in all subprocess calls so that failures are raised
            # as exceptions and logged.
            subprocess.run(
                ["python3", "scripts/config.py", "full"],
                universal_newlines=True,
                check=True
            )
            my_environment = os.environ.copy()
            my_environment["CFLAGS"] = "-fno-asynchronous-unwind-tables"
            # Run make clean separately to lib to prevent unwanted behavior when
            # make is invoked with parallelism.
            subprocess.run(
                ["make", "clean"],
                universal_newlines=True,
                check=True
            )
            subprocess.run(
                ["make", "lib"],
                env=my_environment,
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True
            )

            # Perform object file analysis using nm
            symbols = self.parse_symbols_from_nm([
                "library/libmbedcrypto.a",
                "library/libmbedtls.a",
                "library/libmbedx509.a"
            ])

            subprocess.run(
                ["make", "clean"],
                universal_newlines=True,
                check=True
            )
        except subprocess.CalledProcessError as error:
            self.log.debug(error.output)
            raise error
        finally:
            # Put back the original config regardless of there being errors.
            # Works also for keyboard interrupts.
            shutil.move(
                "include/mbedtls/mbedtls_config.h.bak",
                "include/mbedtls/mbedtls_config.h"
            )

        return symbols

class NameChecker():
    """
    Representation of the core name checking operation performed by this script.
    """
    def __init__(self, parse_result, log):
        self.parse_result = parse_result
        self.log = log

    def perform_checks(self, quiet=False):
        """
        A comprehensive checker that performs each check in order, and outputs
        a final verdict.

        Args:
        * quiet: whether to hide detailed problem explanation.
        """
        self.log.info("=============")
        Problem.quiet = quiet
        problems = 0
        problems += self.check_symbols_declared_in_header()

        pattern_checks = [
            ("public_macros", PUBLIC_MACRO_PATTERN),
            ("internal_macros", INTERNAL_MACRO_PATTERN),
            ("enum_consts", CONSTANTS_PATTERN),
            ("identifiers", IDENTIFIER_PATTERN)
        ]
        for group, check_pattern in pattern_checks:
            problems += self.check_match_pattern(group, check_pattern)

        problems += self.check_for_typos()

        self.log.info("=============")
        if problems > 0:
            self.log.info("FAIL: {0} problem(s) to fix".format(str(problems)))
            if quiet:
                self.log.info("Remove --quiet to see explanations.")
            else:
                self.log.info("Use --quiet for minimal output.")
            return 1
        else:
            self.log.info("PASS")
            return 0

    def check_symbols_declared_in_header(self):
        """
        Perform a check that all detected symbols in the library object files
        are properly declared in headers.
        Assumes parse_names_in_source() was called before this.

        Returns the number of problems that need fixing.
        """
        problems = []
        all_identifiers = self.parse_result["identifiers"] +  \
            self.parse_result["excluded_identifiers"]

        for symbol in self.parse_result["symbols"]:
            found_symbol_declared = False
            for identifier_match in all_identifiers:
                if symbol == identifier_match.name:
                    found_symbol_declared = True
                    break

            if not found_symbol_declared:
                problems.append(SymbolNotInHeader(symbol))

        self.output_check_result("All symbols in header", problems)
        return len(problems)

    def check_match_pattern(self, group_to_check, check_pattern):
        """
        Perform a check that all items of a group conform to a regex pattern.
        Assumes parse_names_in_source() was called before this.

        Args:
        * group_to_check: string key to index into self.parse_result.
        * check_pattern: the regex to check against.

        Returns the number of problems that need fixing.
        """
        problems = []

        for item_match in self.parse_result[group_to_check]:
            if not re.search(check_pattern, item_match.name):
                problems.append(PatternMismatch(check_pattern, item_match))
            # Double underscore should not be used for names
            if re.search(r".*__.*", item_match.name):
                problems.append(
                    PatternMismatch("no double underscore allowed", item_match))

        self.output_check_result(
            "Naming patterns of {}".format(group_to_check),
            problems)
        return len(problems)

    def check_for_typos(self):
        """
        Perform a check that all words in the source code beginning with MBED are
        either defined as macros, or as enum constants.
        Assumes parse_names_in_source() was called before this.

        Returns the number of problems that need fixing.
        """
        problems = []

        # Set comprehension, equivalent to a list comprehension wrapped by set()
        all_caps_names = {
            match.name
            for match
            in self.parse_result["public_macros"] +
            self.parse_result["internal_macros"] +
            self.parse_result["private_macros"] +
            self.parse_result["enum_consts"]
            }
        typo_exclusion = re.compile(r"XXX|__|_$|^MBEDTLS_.*CONFIG_FILE$|"
                                    r"MBEDTLS_TEST_LIBTESTDRIVER*|"
                                    r"PSA_CRYPTO_DRIVER_TEST")

        for name_match in self.parse_result["mbed_psa_words"]:
            found = name_match.name in all_caps_names

            # Since MBEDTLS_PSA_ACCEL_XXX defines are defined by the
            # PSA driver, they will not exist as macros. However, they
            # should still be checked for typos using the equivalent
            # BUILTINs that exist.
            if "MBEDTLS_PSA_ACCEL_" in name_match.name:
                found = name_match.name.replace(
                    "MBEDTLS_PSA_ACCEL_",
                    "MBEDTLS_PSA_BUILTIN_") in all_caps_names

            if not found and not typo_exclusion.search(name_match.name):
                problems.append(Typo(name_match))

        self.output_check_result("Likely typos", problems)
        return len(problems)

    def output_check_result(self, name, problems):
        """
        Write out the PASS/FAIL status of a performed check depending on whether
        there were problems.

        Args:
        * name: the name of the test
        * problems: a List of encountered Problems
        """
        if problems:
            self.log.info("{}: FAIL\n".format(name))
            for problem in problems:
                self.log.warning(str(problem))
        else:
            self.log.info("{}: PASS".format(name))

def main():
    """
    Perform argument parsing, and create an instance of CodeParser and
    NameChecker to begin the core operation.
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "This script confirms that the naming of all symbols and identifiers "
            "in Mbed TLS are consistent with the house style and are also "
            "self-consistent.\n\n"
            "Expected to be run from the Mbed TLS root directory.")
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="show parse results"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="hide unnecessary text, explanations, and highlights"
    )

    args = parser.parse_args()

    # Configure the global logger, which is then passed to the classes below
    log = logging.getLogger()
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    log.addHandler(logging.StreamHandler())

    try:
        if build_tree.looks_like_tf_psa_crypto_root(os.getcwd()):
            tf_psa_crypto_code_parser = TFPSACryptoCodeParser(log)
            parse_result = tf_psa_crypto_code_parser.comprehensive_parse()
        elif build_tree.looks_like_mbedtls_root(os.getcwd()):
            # Mbed TLS uses TF-PSA-Crypto, so we need to parse TF-PSA-Crypto too
            mbedtls_code_parser = MBEDTLSCodeParser(log)
            parse_result = mbedtls_code_parser.comprehensive_parse()
        else:
            raise Exception("This script must be run from Mbed TLS or TF-PSA-Crypto root")
    except Exception: # pylint: disable=broad-except
        traceback.print_exc()
        sys.exit(2)

    name_checker = NameChecker(parse_result, log)
    return_code = name_checker.perform_checks(quiet=args.quiet)

    sys.exit(return_code)

if __name__ == "__main__":
    main()
