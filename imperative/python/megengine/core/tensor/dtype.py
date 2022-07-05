import re
from collections import namedtuple
from typing import Union

import numpy as np

from .._imperative_rt.common import (
    bfloat16,
    get_scale,
    get_zero_point,
    intb1,
    intb2,
    intb4,
    is_dtype_equal,
    is_quantize,
)


def get_dtype_bit(dtype_name: str):
    special_cases = {"bool": 1}
    if dtype_name in special_cases:
        return special_cases[dtype_name]
    numbers = re.findall(r"\d+", dtype_name)
    assert len(numbers) == 1, "Unsupport dtype name with more than one number."
    return int(numbers[0])


# normal dtype related
def is_lowbit(dtype):
    return (dtype is intb1) or (dtype is intb2) or (dtype is intb4)


def is_bfloat16(dtype):
    return dtype is bfloat16


def is_differentible_dtype(dtype):
    return dtype == np.float32 or dtype == np.float16 or is_bfloat16(dtype)


# quantization dtype related

# use namedtuple to make class immutable, comparable and easy to print
class QuantDtypeMeta(
    namedtuple(
        "QuantDtypeMeta",
        ["name", "cname", "np_dtype_str", "qmin", "qmax", "is_unsigned"],
    )
):
    r"""Store metadata for quantize dtype. Could be used to create custom quant dtype
    for QAT when the network don't need to be converted for inference, but only
    to export network metadata for third-party platform inference.

    Args:
        name: a unique name string.
        cname: used in :func:`~.create_quantized_dtype` for model dump and inference.
        np_dtype_str: used in :func:`~.create_quantized_dtype` to generate ``np.dtype``.
        qmin: a int number indicating quant dtype's lowerbound.
        qmax: a int number indicating quant dtype's upperbound.
        is_unsigned: a helper value that could be inference from np_dtype_str.
    """

    def __new__(
        cls,
        name: str,
        cname: str,
        np_dtype_str: str,
        qmin: int,
        qmax: int,
        is_unsigned: bool = None,
    ):
        assert isinstance(np_dtype_str, str)
        is_unsigned = np_dtype_str[0] == "u" if is_unsigned is None else is_unsigned
        return super().__new__(cls, name, cname, np_dtype_str, qmin, qmax, is_unsigned)

    def __copy__(self):
        return self

    def __deepcopy__(self, _):
        r"""
        Ignore deepcopy so that a dtype meta can be treated as singleton, for more
        strict check in :meth:`~.FakeQuantize.fake_quant_forward`.
        """
        return self


_builtin_quant_dtypes = {
    "quint8": QuantDtypeMeta("quint8", "Quantized8Asymm", "uint8", 0, 255),
    "qint8": QuantDtypeMeta("qint8", "QuantizedS8", "int8", -128, 127),
    "qint8_narrow": QuantDtypeMeta("qint8_narrow", "QuantizedS8", "int8", -127, 127),
    "quint4": QuantDtypeMeta("quint4", "Quantized4Asymm", "uint8", 0, 15),
    "qint4": QuantDtypeMeta("qint4", "QuantizedS4", "int8", -8, 7),
    "qint1": QuantDtypeMeta("qint1", "QuantizedS1", "int8", 0, 1),
    "qint32": QuantDtypeMeta(
        "qint32", "QuantizedS32", "int32", -(2 ** 31), 2 ** 31 - 1,
    ),
    # NOTE: int2 is not supported for model dump yet
    "quint2": QuantDtypeMeta("quint2", None, "uint8", 0, 3),
    "qint2": QuantDtypeMeta("qint2", None, "int8", -2, 1),
}


def _check_zero_point(zp: int, dtype_meta: QuantDtypeMeta):
    qmin = dtype_meta.qmin
    qmax = dtype_meta.qmax
    if zp < qmin or zp > qmax:
        raise ValueError(
            "zero_point should be within [{}, {}] for {}".format(
                qmin, qmax, dtype_meta.name
            )
        )


def create_quantized_dtype(
    dtype_meta: QuantDtypeMeta, scale: float, zp: Union[int, None]
):
    r"""Get quantized dtype with metadata attribute according to _metadata_dict.

    Note that unsigned dtype must have ``zero_point`` and signed dtype must
    not have ``zero_point``, to be consitent with tensor generated by calling
    compiled function from `CompGraph.compile(inputs, outspec)`.

    Args:
        dtype_meta: a QuantDtypeMeta indicating which dtype to return. the
            ``cname`` attribute cannot be ``None``.
        scale: a number for scale to store in dtype's metadata
        zp: a number for zero_point to store in dtype's metadata
    """
    if dtype_meta.cname is None:
        raise ValueError("dtype {} without cname attr is not supported.")
    if dtype_meta.is_unsigned:
        if zp is None or int(zp) != zp:
            raise ValueError("zero_point should be an integer")
        zp = int(zp)
        _check_zero_point(zp, dtype_meta)
        return np.dtype(
            dtype_meta.np_dtype_str,
            metadata={
                "mgb_dtype": {
                    "name": dtype_meta.cname,
                    "scale": float(scale),
                    "zero_point": zp,
                }
            },
        )
    else:
        # Don't trick to combine with is_unsigned. Metadata should not contain
        # invalid field to keep consistent with c dtype.
        return np.dtype(
            dtype_meta.np_dtype_str,
            metadata={"mgb_dtype": {"name": dtype_meta.cname, "scale": float(scale)}},
        )


def quint8(scale, zero_point):
    r"""Consturct a quantized unsigned int8 data type with ``scale`` (float) and
    ``zero_point`` (uint8). The real value represented by a quint8 data type is
    float_val = scale * (uint8_val - zero_point)
    """
    return create_quantized_dtype(_builtin_quant_dtypes["quint8"], scale, zero_point)


def qint8(scale):
    r"""Construct a quantized int8 data type with ``scale`` (float). The real value
    represented by a qint8 data type is float_val = scale * int8_val
    """
    return create_quantized_dtype(_builtin_quant_dtypes["qint8"], scale, None)


def qint32(scale):
    r"""Construct a quantized int32 data type with ``scale`` (float). The real value
    represented by a qint32 data type is float_val = scale * int32_val
    """
    return create_quantized_dtype(_builtin_quant_dtypes["qint32"], scale, None)


def quint4(scale, zero_point):
    r"""Consturct a quantized unsigned int4 data type with ``scale`` (float) and
    ``zero_point`` (uint8). The real value represented by a quint4 data type is
    float_val = scale * (uint4_val - zero_point)
    """
    return create_quantized_dtype(_builtin_quant_dtypes["quint4"], scale, zero_point)


def qint4(scale):
    r"""Construct a quantized int4 data type with ``scale`` (float). The real value
    represented by a qint4 data type is float_val = scale * int4_val
    """
    return create_quantized_dtype(_builtin_quant_dtypes["qint4"], scale, None)


def qint1(scale):
    r"""Construct a quantized int1 data type with ``scale`` (float). The real value
    represented by a qint1 data type is float_val = scale * int1_val
    """
    return create_quantized_dtype(_builtin_quant_dtypes["qint1"], scale, None)


def _convert_to_quantized_dtype(
    arr: np.ndarray, dtype: np.dtype, dtype_meta: QuantDtypeMeta
):
    if not isinstance(arr, np.ndarray):
        raise ValueError("arr parameter should be instance of np.ndarray")
    if (
        not is_quantize(dtype)
        or dtype.metadata["mgb_dtype"]["name"] != dtype_meta.cname
    ):
        raise ValueError("dtype parameter should be a {} dtype".format(dtype_meta))
    arr_metadata = dtype.metadata["mgb_dtype"]
    if dtype_meta.is_unsigned:
        scale, zp = (
            arr_metadata["scale"],
            arr_metadata["zero_point"],
        )
        return (
            (np.round(arr / scale) + zp)
            .clip(dtype_meta.qmin, dtype_meta.qmax)
            .astype(dtype)
        )
    else:
        # don't trick to combine with is_unsigned, seeing ``get_quantized_dtype``
        scale = arr_metadata["scale"]
        return (
            np.round(arr / scale).clip(dtype_meta.qmin, dtype_meta.qmax).astype(dtype)
        )


def _convert_from_quantized_dtype(arr: np.ndarray, dtype_meta: QuantDtypeMeta):
    if not isinstance(arr, np.ndarray):
        raise ValueError("arr parameter should be instance of np.ndarray")
    if (
        not is_quantize(arr.dtype)
        or arr.dtype.metadata["mgb_dtype"]["name"] != dtype_meta.cname
    ):
        raise ValueError("arr's dtype should be a {} dtype".format(dtype_meta))
    arr_metadata = arr.dtype.metadata["mgb_dtype"]
    if dtype_meta.is_unsigned:
        scale, zp = (
            arr_metadata["scale"],
            arr_metadata["zero_point"],
        )
        return (arr.astype(np.float32) - zp) * scale
    else:
        # don't trick to combine with is_unsigned, seeing ``get_quantized_dtype``
        scale = arr_metadata["scale"]
        return (arr.astype(np.float32)) * scale


def convert_to_quint8(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a quint8 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a quint8.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["quint8"])


def convert_from_quint8(arr: np.ndarray):
    r"""Dequantize a quint8 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["quint8"])


def convert_to_qint8(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a qint8 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a qint8.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["qint8"])


def convert_from_qint8(arr: np.ndarray):
    r"""Dequantize a qint8 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["qint8"])


def convert_to_qint32(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a qint32 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a qint8.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["qint32"])


def convert_from_qint32(arr):
    r"""Dequantize a qint32 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["qint32"])


def convert_to_quint4(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a quint4 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a quint4.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["quint4"])


def convert_from_quint4(arr: np.ndarray):
    r"""Dequantize a quint4 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["quint4"])


def convert_to_qint4(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a qint4 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a qint4.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["qint4"])


def convert_from_qint4(arr: np.ndarray):
    r"""Dequantize a qint4 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["qint4"])


def convert_to_qint1(arr: np.ndarray, q: np.dtype):
    r"""Quantize a float NumPy ndarray into a qint1 one with specified params.

    Args:
        arr: Input ndarray.
        q: Target data type, should be a qint1.
    """
    return _convert_to_quantized_dtype(arr, q, _builtin_quant_dtypes["qint1"])


def convert_from_qint1(arr: np.ndarray):
    r"""Dequantize a qint1 NumPy ndarray into a float one.

    Args:
        arr: Input ndarray.
    """
    return _convert_from_quantized_dtype(arr, _builtin_quant_dtypes["qint1"])
