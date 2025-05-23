"""Framework classes for generation of bignum mod_raw test cases."""
# Copyright The Mbed TLS Contributors
# SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-or-later
#

from typing import Iterator, List

from . import test_case
from . import test_data_generation
from . import bignum_common
from .bignum_data import ONLY_PRIME_MODULI

class BignumModRawTarget(test_data_generation.BaseTarget):
    #pylint: disable=abstract-method, too-few-public-methods
    """Target for bignum mod_raw test case generation."""
    target_basename = 'test_suite_bignum_mod_raw.generated'


class BignumModRawSub(bignum_common.ModOperationCommon,
                      BignumModRawTarget):
    """Test cases for bignum mpi_mod_raw_sub()."""
    symbol = "-"
    test_function = "mpi_mod_raw_sub"
    test_name = "mbedtls_mpi_mod_raw_sub"
    input_style = "fixed"
    arity = 2

    def arguments(self) -> List[str]:
        return [bignum_common.quote_str(n) for n in [self.arg_a,
                                                     self.arg_b,
                                                     self.arg_n]
               ] + self.result()

    def result(self) -> List[str]:
        result = (self.int_a - self.int_b) % self.int_n
        return [self.format_result(result)]

class BignumModRawFixQuasiReduction(bignum_common.ModOperationCommon,
                                    BignumModRawTarget):
    """Test cases for ecp quasi_reduction()."""
    symbol = "-"
    test_function = "mpi_mod_raw_fix_quasi_reduction"
    test_name = "fix_quasi_reduction"
    input_style = "fixed"
    arity = 1

    # Extend the default values with n < x < 2n
    input_values = bignum_common.ModOperationCommon.input_values + [
        "73",

        # First number generated by random.getrandbits(1024) - seed(3,2)
        "ea7b5bf55eb561a4216363698b529b4a97b750923ceb3ffd",

        # First number generated by random.getrandbits(1024) - seed(1,2)
        ("cd447e35b8b6d8fe442e3d437204e52db2221a58008a05a6c4647159c324c985" +
         "9b810e766ec9d28663ca828dd5f4b3b2e4b06ce60741c7a87ce42c8218072e8c" +
         "35bf992dc9e9c616612e7696a6cecc1b78e510617311d8a3c2ce6f447ed4d57b" +
         "1e2feb89414c343c1027c4d1c386bbc4cd613e30d8f16adf91b7584a2265b1f5")
    ] # type: List[str]

    def result(self) -> List[str]:
        result = self.int_a % self.int_n
        return [self.format_result(result)]

    @property
    def is_valid(self) -> bool:
        return bool(self.int_a < 2 * self.int_n)

class BignumModRawMul(bignum_common.ModOperationCommon,
                      BignumModRawTarget):
    """Test cases for bignum mpi_mod_raw_mul()."""
    symbol = "*"
    test_function = "mpi_mod_raw_mul"
    test_name = "mbedtls_mpi_mod_raw_mul"
    input_style = "arch_split"
    arity = 2

    def arguments(self) -> List[str]:
        return [self.format_result(self.to_montgomery(self.int_a)),
                self.format_result(self.to_montgomery(self.int_b)),
                bignum_common.quote_str(self.arg_n)
               ] + self.result()

    def result(self) -> List[str]:
        result = (self.int_a * self.int_b) % self.int_n
        return [self.format_result(self.to_montgomery(result))]


class BignumModRawInvPrime(bignum_common.ModOperationCommon,
                           BignumModRawTarget):
    """Test cases for bignum mpi_mod_raw_inv_prime()."""
    moduli = ONLY_PRIME_MODULI
    symbol = "^ -1"
    test_function = "mpi_mod_raw_inv_prime"
    test_name = "mbedtls_mpi_mod_raw_inv_prime (Montgomery form only)"
    input_style = "arch_split"
    arity = 1
    suffix = True
    montgomery_form_a = True
    disallow_zero_a = True

    def result(self) -> List[str]:
        result = bignum_common.invmod_positive(self.int_a, self.int_n)
        mont_result = self.to_montgomery(result)
        return [self.format_result(mont_result)]


class BignumModRawAdd(bignum_common.ModOperationCommon,
                      BignumModRawTarget):
    """Test cases for bignum mpi_mod_raw_add()."""
    symbol = "+"
    test_function = "mpi_mod_raw_add"
    test_name = "mbedtls_mpi_mod_raw_add"
    input_style = "fixed"
    arity = 2

    def result(self) -> List[str]:
        result = (self.int_a + self.int_b) % self.int_n
        return [self.format_result(result)]


class BignumModRawConvertRep(bignum_common.ModOperationCommon,
                             BignumModRawTarget):
    # This is an abstract class, it's ok to have unimplemented methods.
    #pylint: disable=abstract-method
    """Test cases for representation conversion."""
    symbol = ""
    input_style = "arch_split"
    arity = 1
    rep = bignum_common.ModulusRepresentation.INVALID

    def set_representation(self, r: bignum_common.ModulusRepresentation) -> None:
        self.rep = r

    def arguments(self) -> List[str]:
        return ([bignum_common.quote_str(self.arg_n), self.rep.symbol(),
                 bignum_common.quote_str(self.arg_a)] +
                self.result())

    def description(self) -> str:
        base = super().description()
        mod_with_rep = 'mod({})'.format(self.rep.name)
        return base.replace('mod', mod_with_rep, 1)

    @classmethod
    def test_cases_for_values(cls, rep: bignum_common.ModulusRepresentation,
                              n: str, a: str) -> Iterator[test_case.TestCase]:
        """Emit test cases for the given values (if any).

        This may emit no test cases if a isn't valid for the modulus n,
        or multiple test cases if rep requires different data depending
        on the limb size.
        """
        for bil in cls.limb_sizes:
            test_object = cls(n, a, bits_in_limb=bil)
            test_object.set_representation(rep)
            # The class is set to having separate test cases for each limb
            # size, because the Montgomery representation requires it.
            # But other representations don't require it. So for other
            # representations, emit a single test case with no dependency
            # on the limb size.
            if rep is not bignum_common.ModulusRepresentation.MONTGOMERY:
                test_object.dependencies = \
                    [dep for dep in test_object.dependencies
                     if not dep.startswith('MBEDTLS_HAVE_INT')]
            if test_object.is_valid:
                yield test_object.create_test_case()
            if rep is not bignum_common.ModulusRepresentation.MONTGOMERY:
                # A single test case (emitted, or skipped due to invalidity)
                # is enough, since this test case doesn't depend on the
                # limb size.
                break

    # The parent class doesn't support non-bignum parameters. So we override
    # test generation, in order to have the representation as a parameter.
    @classmethod
    def generate_function_tests(cls) -> Iterator[test_case.TestCase]:

        for rep in bignum_common.ModulusRepresentation.supported_representations():
            for n in cls.moduli:
                for a in cls.input_values:
                    yield from cls.test_cases_for_values(rep, n, a)

class BignumModRawCanonicalToModulusRep(BignumModRawConvertRep):
    """Test cases for mpi_mod_raw_canonical_to_modulus_rep."""
    test_function = "mpi_mod_raw_canonical_to_modulus_rep"
    test_name = "Rep canon->mod"

    def result(self) -> List[str]:
        return [self.format_result(self.convert_from_canonical(self.int_a, self.rep))]

class BignumModRawModulusToCanonicalRep(BignumModRawConvertRep):
    """Test cases for mpi_mod_raw_modulus_to_canonical_rep."""
    test_function = "mpi_mod_raw_modulus_to_canonical_rep"
    test_name = "Rep mod->canon"

    @property
    def arg_a(self) -> str:
        return self.format_arg("{:x}".format(self.convert_from_canonical(self.int_a, self.rep)))

    def result(self) -> List[str]:
        return [self.format_result(self.int_a)]


class BignumModRawConvertToMont(bignum_common.ModOperationCommon,
                                BignumModRawTarget):
    """ Test cases for mpi_mod_raw_to_mont_rep(). """
    test_function = "mpi_mod_raw_to_mont_rep"
    test_name = "Convert into Mont: "
    symbol = "R *"
    input_style = "arch_split"
    arity = 1

    def result(self) -> List[str]:
        result = self.to_montgomery(self.int_a)
        return [self.format_result(result)]

class BignumModRawConvertFromMont(bignum_common.ModOperationCommon,
                                  BignumModRawTarget):
    """ Test cases for mpi_mod_raw_from_mont_rep(). """
    test_function = "mpi_mod_raw_from_mont_rep"
    test_name = "Convert from Mont: "
    symbol = "1/R *"
    input_style = "arch_split"
    arity = 1

    def result(self) -> List[str]:
        result = self.from_montgomery(self.int_a)
        return [self.format_result(result)]

class BignumModRawModNegate(bignum_common.ModOperationCommon,
                            BignumModRawTarget):
    """ Test cases for mpi_mod_raw_neg(). """
    test_function = "mpi_mod_raw_neg"
    test_name = "Modular negation: "
    symbol = "-"
    input_style = "arch_split"
    arity = 1

    def result(self) -> List[str]:
        result = (self.int_n - self.int_a) % self.int_n
        return [self.format_result(result)]
