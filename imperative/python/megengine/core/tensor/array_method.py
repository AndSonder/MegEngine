# -*- coding: utf-8 -*-
# MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
#
# Copyright (c) 2014-2021 Megvii Inc. All rights reserved.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
import abc
import collections
from functools import lru_cache
from typing import Union

import numpy as np

from .. import _config
from .._imperative_rt.common import CompNode
from .._imperative_rt.core2 import (
    SymbolVar,
    Tensor,
    apply,
    astype_cpp,
    batched_matmul_cpp,
    broadcast_cpp,
    getitem_cpp,
    matmul_cpp,
)
from .._imperative_rt.core2 import reduce_to_scalar as _reduce_to_scalar
from .._imperative_rt.core2 import reshape_cpp, setitem_cpp, squeeze_cpp, transpose_cpp
from ..ops import builtin
from . import amp
from .utils import _normalize_axis, astensor1d, cast_tensors, make_shape_tuple, subgraph

_ElwMod = builtin.Elemwise.Mode


def _elwise_apply(args, mode):
    op = builtin.Elemwise(mode)
    (result,) = apply(op, *args)
    return result


def _elwise(*args, mode):
    return _elwise_apply(args, mode)


@lru_cache(maxsize=None)
def _get_extentedMatrixMulOp(
    device, dtype, dim1, dim2, transpose_a, transpose_b, compute_mode, format, strategy,
):
    @subgraph("extentedMatrixMulOp", dtype, device, 2, gopt_level=2)
    def extentedMatrixMulOp(inputs, f, c):
        assert len(inputs) == 2
        inp1, inp2 = inputs
        _dim1, _dim2 = dim1, dim2

        def build_shape_head(shape, idx=-1):
            # shape[:idx]
            return f(
                builtin.Subtensor(items=[[0, False, True, False, False]]),
                shape,
                c(idx, "int32"),
            )

        def build_shape_tail(shape, idx=-1):
            # shape[idx:]
            return f(
                builtin.Subtensor(items=[[0, True, False, False, False]]),
                shape,
                c(idx, "int32"),
            )

        remove_row, remove_col = False, False
        if _dim1 == 1:
            _dim1 = 2
            remove_row = True
        if _dim2 == 1:
            _dim2 = 2
            remove_col = True

        if remove_row:
            inp1 = f(builtin.AddAxis(axis=[0,]), inp1)
        if remove_col:
            inp2 = f(builtin.AddAxis(axis=[1,]), inp2)

        shape1 = f(builtin.GetVarShape(), inp1)
        shape2 = f(builtin.GetVarShape(), inp2)
        if _dim1 > 2:
            inp1 = f(
                builtin.Reshape(),
                inp1,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    f(builtin.Reduce(mode="product", axis=0), build_shape_head(shape1)),
                    build_shape_tail(shape1),
                ),
            )
        if _dim2 > 2:
            inp2 = f(
                builtin.Reshape(),
                inp2,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    f(builtin.Reduce(mode="product", axis=0), build_shape_head(shape2)),
                    build_shape_tail(shape2),
                ),
            )
        op = builtin.MatrixMul(
            transposeA=transpose_a,
            transposeB=transpose_b,
            compute_mode=compute_mode,
            format=format,
            strategy=strategy.value,
        )
        result = f(op, inp1, inp2)
        result_shape = f(builtin.GetVarShape(), result)
        if _dim1 > 2:
            result = f(
                builtin.Reshape(),
                result,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    build_shape_head(shape1),
                    build_shape_tail(result_shape),
                ),
            )
        if _dim2 > 2:
            result = f(
                builtin.Reshape(),
                result,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    build_shape_head(shape2),
                    build_shape_tail(result_shape),
                ),
            )
        maxdim = _dim1 if _dim1 > _dim2 else _dim2
        if remove_row:
            result = f(builtin.RemoveAxis(axis=[maxdim - 2]), result)
        if remove_col:
            result = f(builtin.RemoveAxis(axis=[maxdim - 1]), result)
        return (result,), (True,)

    return extentedMatrixMulOp


@lru_cache(maxsize=None)
def _get_extentedBatchedMatrixMulOp(
    device, dtype, dim1, dim2, transpose_a, transpose_b, compute_mode, format, strategy,
):
    @subgraph("extentedBatchedMatrixMulOp", dtype, device, 2, gopt_level=2)
    def extentedBatchedMatrixMulOp(inputs, f, c):
        assert len(inputs) == 2
        inp1, inp2 = inputs
        _dim1, _dim2 = dim1, dim2

        def build_shape_head(shape, idx=-2):
            # shape[:idx]
            return f(
                builtin.Subtensor(items=[[0, False, True, False, False]]),
                shape,
                c(idx, "int32"),
            )

        def build_shape_tail(shape, idx=-2):
            # shape[idx:]
            return f(
                builtin.Subtensor(items=[[0, True, False, False, False]]),
                shape,
                c(idx, "int32"),
            )

        remove_row, remove_col = False, False
        if _dim1 == 1:
            _dim1 = 2
            remove_row = True
        if _dim2 == 1:
            _dim2 = 2
            remove_col = True

        if remove_row:
            inp1 = f(builtin.AddAxis(axis=[0,]), inp1)
        if remove_col:
            inp2 = f(builtin.AddAxis(axis=[1,]), inp2)
        shape1 = f(builtin.GetVarShape(), inp1)
        shape2 = f(builtin.GetVarShape(), inp2)
        maxdim = _dim1 if _dim1 > _dim2 else _dim2
        if _dim1 > _dim2:
            # broadcast
            shape2 = f(
                builtin.Concat(axis=0, comp_node=device),
                build_shape_head(shape1, idx=-_dim2),  # shape1[:-_dim2]
                shape2,
            )
            inp2 = f(builtin.Broadcast(), inp2, shape2)
            batch_shape = build_shape_head(shape1)
        if _dim2 > _dim1:
            # broadcast
            shape1 = f(
                builtin.Concat(axis=0, comp_node=device),
                build_shape_head(shape2, idx=-_dim1),  # shape2[:-_dim1]
                shape1,
            )
            inp1 = f(builtin.Broadcast(), inp1, shape1)
            batch_shape = build_shape_head(shape2)
        if _dim1 == _dim2:
            batch_shape = build_shape_head(shape1)

        # compress inputs to 3d
        if maxdim > 3:
            inp1 = f(
                builtin.Reshape(),
                inp1,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    f(builtin.Reduce(mode="product", axis=0), batch_shape),
                    build_shape_tail(shape1),
                ),
            )
            inp2 = f(
                builtin.Reshape(),
                inp2,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    f(builtin.Reduce(mode="product", axis=0), batch_shape),
                    build_shape_tail(shape2),
                ),
            )
        op = builtin.BatchedMatrixMul(
            transposeA=transpose_a,
            transposeB=transpose_b,
            compute_mode=compute_mode,
            format=format,
            strategy=strategy.value,
        )
        result = f(op, inp1, inp2)

        if maxdim > 3:
            result = f(
                builtin.Reshape(),
                result,
                f(
                    builtin.Concat(axis=0, comp_node=device),
                    batch_shape,
                    build_shape_tail(f(builtin.GetVarShape(), result)),
                ),
            )
        if remove_row:
            result = f(builtin.RemoveAxis(axis=[maxdim - 2]), result)
        if remove_col:
            result = f(builtin.RemoveAxis(axis=[maxdim - 1]), result)
        return (result,), (True,)

    return extentedBatchedMatrixMulOp


class _Hashable:
    def __init__(self, value) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(str(self.value))

    def __eq__(self, o: object) -> bool:
        if not isinstance(o, _Hashable):
            return False
        return self.value == o.value


def symbolicMatrixMul(
    inp1, inp2, dim1, dim2, transpose_a, transpose_b, compute_mode, format, strategy
):
    extentedMatrixMulOp = _get_extentedMatrixMulOp(
        inp1.device,
        inp1.dtype,
        dim1,
        dim2,
        transpose_a,
        transpose_b,
        compute_mode,
        format,
        strategy=_Hashable(strategy),
    )
    (result,) = apply(extentedMatrixMulOp(), inp1, inp2)
    return result


def symbolicBatchedMatrixMul(
    inp1, inp2, dim1, dim2, transpose_a, transpose_b, compute_mode, format, strategy
):
    extentedBatchedMatrixMulOp = _get_extentedBatchedMatrixMulOp(
        inp1.device,
        inp1.dtype,
        dim1,
        dim2,
        transpose_a,
        transpose_b,
        compute_mode,
        format,
        strategy=_Hashable(strategy),
    )
    (result,) = apply(extentedBatchedMatrixMulOp(), inp1, inp2)
    return result


def _matmul(
    inp1,
    inp2,
    transpose_a=False,
    transpose_b=False,
    compute_mode="default",
    format="default",
):
    dim1, dim2 = inp1.ndim, inp2.ndim
    assert dim1 > 0 and dim2 > 0
    maxdim = dim1 if dim1 > dim2 else dim2
    compute_mode = _config._get_actual_op_param(compute_mode, _config.__compute_mode)

    Strategy = builtin.ops.MatrixMul.Strategy
    strategy = Strategy(0)
    if _config._benchmark_kernel:
        strategy |= Strategy.PROFILE
    else:
        strategy |= Strategy.HEURISTIC
    if _config._deterministic_kernel:
        strategy |= Strategy.REPRODUCIBLE

    if dim1 == 1 and dim2 == 1:  # dispatch to Dot
        (result,) = apply(builtin.Dot(), inp1, inp2)
        return result
    elif maxdim <= 2 or (dim2 <= 2 and not transpose_a):  # dispath to MatrixMul
        # 2x1
        # 1x2
        # 2x2
        # nx1(transpose_a=False), n>=3
        # nx2(transpose_a=False), n>=3
        return matmul_cpp(
            inp1,
            inp2,
            dim1,
            dim2,
            transpose_a,
            transpose_b,
            compute_mode,
            format,
            _config._benchmark_kernel,
            _config._deterministic_kernel,
            strategy,
            symbolicMatrixMul,
        )
    else:  # dispath to BatchedMatrixMul
        # nx1(transpose_a=True), n>=3
        # nx2(transpose_a=True), n>=3
        # nxm,n>=3,m>=3
        # 1xm,m>=3
        # 2xm,m>=3
        return batched_matmul_cpp(
            inp1,
            inp2,
            dim1,
            dim2,
            transpose_a,
            transpose_b,
            compute_mode,
            format,
            _config._benchmark_kernel,
            _config._deterministic_kernel,
            strategy,
            symbolicBatchedMatrixMul,
        )


def _unary_elwise(mode):
    def f(self):
        return _elwise(self, mode=mode)

    return f


def _binary_elwise(mode, rev=False):
    if not rev:

        def f(self, value):
            return _elwise(self, value, mode=mode)

    else:

        def f(self, value):
            return _elwise(value, self, mode=mode)

    return f


def _logical_unary_elwise(mode, rev=False):
    def f(self):
        if self.dtype != np.bool_:
            raise TypeError("{} requires a bool tensor".format(mode))
        return _elwise(self, mode=mode)

    return f


def _logical_binary_elwise(mode, rev=False):
    if not rev:

        def f(self, value):
            if self.dtype != np.bool_ or value.dtype != np.bool_:
                raise TypeError("{} requires 2 bool tensors".format(mode))
            return _elwise(self, value, mode=mode)

    else:

        def f(self, value):
            if self.dtype != np.bool_ or value.dtype != np.bool_:
                raise TypeError("{} requires 2 bool tensors".format(mode))
            return _elwise(value, self, mode=mode)

    return f


def _reduce(mode):
    def f(self, axis=None, keepdims: bool = False):
        data = self
        if axis is None:
            assert not keepdims, "can not set axis=None and keepdims=True"
            (result,) = apply(builtin.Reduce(mode=mode), data)
        elif isinstance(axis, collections.abc.Iterable):
            axis = _normalize_axis(self.ndim, axis, reverse=True)
            for ai in axis:
                op = builtin.Reduce(mode=mode, axis=ai, keepdim=keepdims)
                (data,) = apply(op, data)
            result = data
        else:
            # builtin.Reduce already accept negtive axis
            op = builtin.Reduce(mode=mode, axis=axis, keepdim=keepdims)
            (result,) = apply(op, data)

        return result

    return f


def _inplace(f):
    def g(self, value):
        result = f(self, value)
        if result is NotImplemented:
            raise NotImplementedError
        self._reset(result)
        return self

    return g


def _todo(*_):
    raise NotImplementedError


def _expand_args(args):
    if len(args) == 1:
        if isinstance(
            args[0], (collections.abc.Sequence, Tensor, SymbolVar, np.ndarray),
        ):
            args = args[0]
    return args


class ArrayMethodMixin(abc.ABC):

    # enable tensor to be converted to numpy array
    __array_priority__ = 1001

    def __array__(self, dtype=None):
        if dtype == None:
            return self.numpy()
        return self.numpy().astype(dtype)

    def __array_wrap__(self, array):
        Wrapper = type(self)
        return Wrapper(array, dtype=array.dtype, device=self.device)

    @abc.abstractmethod
    def _reset(self, other):
        pass

    @abc.abstractproperty
    def dtype(self) -> np.dtype:
        pass

    @abc.abstractproperty
    def shape(self) -> Union[tuple, Tensor]:
        pass

    @abc.abstractproperty
    def _tuple_shape(self) -> tuple:
        pass

    @abc.abstractmethod
    def numpy(self) -> np.ndarray:
        pass

    __hash__ = None  # due to __eq__ diviates from python convention

    __lt__ = lambda self, value: _elwise(self, value, mode=_ElwMod.LT).astype("bool")
    __le__ = lambda self, value: _elwise(self, value, mode=_ElwMod.LEQ).astype("bool")
    __gt__ = lambda self, value: _elwise(value, self, mode=_ElwMod.LT).astype("bool")
    __ge__ = lambda self, value: _elwise(value, self, mode=_ElwMod.LEQ).astype("bool")
    __eq__ = lambda self, value: _elwise(self, value, mode=_ElwMod.EQ).astype("bool")
    __ne__ = lambda self, value: _elwise(
        _elwise(self, value, mode=_ElwMod.EQ).astype("bool"), mode=_ElwMod.NOT,
    )

    __neg__ = _unary_elwise(_ElwMod.NEGATE)
    __pos__ = lambda self: self
    __abs__ = _unary_elwise(_ElwMod.ABS)
    __invert__ = _logical_unary_elwise(_ElwMod.NOT)
    __round__ = _unary_elwise(_ElwMod.ROUND)
    __trunc__ = _todo
    __floor__ = _unary_elwise(_ElwMod.FLOOR)
    __ceil__ = _unary_elwise(_ElwMod.CEIL)

    __add__ = _binary_elwise(_ElwMod.ADD)
    __sub__ = _binary_elwise(_ElwMod.SUB)
    __mul__ = _binary_elwise(_ElwMod.MUL)
    __matmul__ = lambda self, other: _matmul(self, other)
    __truediv__ = _binary_elwise(_ElwMod.TRUE_DIV)
    __floordiv__ = _binary_elwise(_ElwMod.FLOOR_DIV)
    __mod__ = _binary_elwise(_ElwMod.MOD)
    # __divmode__
    __pow__ = _binary_elwise(_ElwMod.POW)
    __lshift__ = _binary_elwise(_ElwMod.SHL)
    __rshift__ = _binary_elwise(_ElwMod.SHR)
    __and__ = _logical_binary_elwise(_ElwMod.AND)
    __or__ = _logical_binary_elwise(_ElwMod.OR)
    __xor__ = _logical_binary_elwise(_ElwMod.XOR)

    __radd__ = _binary_elwise(_ElwMod.ADD, rev=1)
    __rsub__ = _binary_elwise(_ElwMod.SUB, rev=1)
    __rmul__ = _binary_elwise(_ElwMod.MUL, rev=1)
    __rmatmul__ = lambda self, other: _matmul(other, self)
    __rtruediv__ = _binary_elwise(_ElwMod.TRUE_DIV, rev=1)
    __rfloordiv__ = _binary_elwise(_ElwMod.FLOOR_DIV, rev=1)
    __rmod__ = _binary_elwise(_ElwMod.MOD, rev=1)
    # __rdivmode__
    __rpow__ = _binary_elwise(_ElwMod.POW, rev=1)
    __rlshift__ = _binary_elwise(_ElwMod.SHL, rev=1)
    __rrshift__ = _binary_elwise(_ElwMod.SHR, rev=1)
    __rand__ = _logical_binary_elwise(_ElwMod.AND, rev=1)
    __ror__ = _logical_binary_elwise(_ElwMod.OR, rev=1)
    __rxor__ = _logical_binary_elwise(_ElwMod.XOR, rev=1)

    __iadd__ = _inplace(__add__)
    __isub__ = _inplace(__sub__)
    __imul__ = _inplace(__mul__)
    __imatmul__ = _inplace(__matmul__)
    __itruediv__ = _inplace(__truediv__)
    __ifloordiv__ = _inplace(__floordiv__)
    __imod__ = _inplace(__mod__)
    __ipow__ = _inplace(__pow__)
    __ilshift__ = _inplace(__lshift__)
    __irshift__ = _inplace(__rshift__)
    __iand__ = _inplace(__and__)
    __ior__ = _inplace(__or__)
    __ixor__ = _inplace(__xor__)

    __index__ = lambda self: self.item().__index__()
    __bool__ = lambda self: bool(self.item())
    __int__ = lambda self: int(self.item())
    __float__ = lambda self: float(self.item())
    __complex__ = lambda self: complex(self.item())

    def __len__(self):
        shape = self._tuple_shape
        if shape:
            return int(shape[0])
        raise TypeError("ndim is 0")

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, index):
        return getitem_cpp(self, index)

    def __setitem__(self, index, value):
        if index is not Ellipsis:
            value = setitem_cpp(self, index, value)
        self._reset(value)

    __contains__ = _todo

    @property
    def ndim(self):
        r"""Returns the number of dimensions of self :class:`~.Tensor`."""
        shape = self._tuple_shape
        if shape is None:
            raise ValueError("unkown ndim")
        return len(shape)

    @property
    def size(self):
        r"""Returns the size of the self :class:`~.Tensor`.
        The returned value is a subclass of :class:`tuple`.
        """
        shape = self.shape
        if shape.__class__ is tuple:
            return np.prod(self.shape).item()
        return shape.prod()

    @property
    def T(self):
        r"""alias of :attr:`~.Tensor.transpose`."""
        return self.transpose()

    def item(self, *args):
        r"""Returns the value of this :class:`~.Tensor` as a standard Python :class:`numbers.Number`.
        This only works for tensors with one element. For other cases, see :meth:`~.tolist`.
        """
        if not args:
            if isinstance(self.size, int):
                assert self.size == 1
            return self.numpy().item()
        return self[args].item()

    def tolist(self):
        r"""Returns the tensor as a (nested) list.
        For scalars, a standard Python number is returned, just like with :meth:`~.item`.
        Tensors are automatically moved to the CPU first if necessary.

        This operation is not differentiable.
        """
        return self.numpy().tolist()

    def astype(self, dtype):
        r"""Returns a :class:`Tensor` with the same data and number of elements
        with the specified :attr:`~.Tensor.dtype`.
        """
        return astype_cpp(self, dtype)

    def reshape(self, *args):
        r"""See :func:`~.reshape`."""
        return reshape_cpp(self, args)

    # FIXME: remove this method
    def _broadcast(self, *args):
        return broadcast_cpp(self, args)

    def transpose(self, *args):
        r"""See :func:`~.transpose`."""
        return transpose_cpp(self, args)

    def flatten(self):
        r"""See :func:`~.flatten`."""
        return reshape_cpp(self, (-1,))

    def sum(self, axis=None, keepdims: bool = False):
        r"""See :func:`~.sum`."""
        return _reduce("sum")(self, axis, keepdims)

    def prod(self, axis=None, keepdims: bool = False):
        r"""See :func:`~.prod`."""
        return _reduce("product")(self, axis, keepdims)

    def min(self, axis=None, keepdims: bool = False):
        r"""See :func:`~.min`."""
        return _reduce("min")(self, axis, keepdims)

    def max(self, axis=None, keepdims: bool = False):
        r"""See :func:`~.max`."""
        return _reduce("max")(self, axis, keepdims)

    def mean(self, axis=None, keepdims: bool = False):
        r"""See :func:`~.mean`."""
        return _reduce("mean")(self, axis, keepdims)
