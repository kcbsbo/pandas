# pylint: disable=E1101,E1103
# pylint: disable=W0212,W0703,W0231,W0622

from cStringIO import StringIO
import sys

from numpy import NaN
import numpy as np

from pandas.core.common import (_pickle_array, _unpickle_array, _try_sort)
from pandas.core.frame import (DataFrame, extract_index,
                               _default_index, _ensure_index)
from pandas.core.index import Index, NULL_INDEX
from pandas.core.series import Series
import pandas.core.common as common
import pandas.core.datetools as datetools
import pandas.lib.tseries as tseries

#-------------------------------------------------------------------------------
# DataMatrix class

class DataMatrix(DataFrame):
    """
    Matrix version of DataFrame, optimized for cross-section operations,
    numerical computation, and other operations that do not require the frame to
    change size.

    Parameters
    ----------
    data : numpy ndarray or dict of sequence-like objects
        Dict can contain Series, arrays, or list-like objects
        Constructor can understand various kinds of inputs
    index : Index or array-like
        Index to use for resulting frame (optional if provided dict of Series)
    columns : Index or array-like
        Required if data is ndarray
    dtype : dtype, default None (infer)
        Data type to force

    Notes
    -----
    Most operations are faster with DataMatrix. You should use it primarily
    unless you are doing a lot of column insertion / deletion (which causes the
    underlying ndarray to have to be reallocated!).
    """
    objects = None
    def __init__(self, data=None, index=None, columns=None):
        if isinstance(data, dict) and len(data) > 0:
            index, columns, block_manager = _init_dict(data, index, columns)
        elif isinstance(data, (np.ndarray, list)):
            index, columns, block_manager = _init_matrix(data, index, columns)
        elif data is None or len(data) == 0:
            raise Exception('TODO!')
        else:
            raise Exception('DataMatrix constructor not properly called!')

        self._data = block_manager
        self.index = index
        self.columns = columns

    def _get_values(self):
        return self._data.as_matrix()

    def _set_values(self, values):
        raise Exception('Values cannot be assigned to')

    values = property(fget=_get_values)

    def _init_matrix(self, values, index, columns, dtype):
        if not isinstance(values, np.ndarray):
            arr = np.array(values)
            if issubclass(arr.dtype.type, basestring):
                arr = np.array(values, dtype=object, copy=True)

            values = arr

        if values.ndim == 1:
            N = values.shape[0]
            if N == 0:
                values = values.reshape((values.shape[0], 0))
            else:
                values = values.reshape((values.shape[0], 1))

        if dtype is not None:
            try:
                values = values.astype(dtype)
            except Exception:
                pass

        N, K = values.shape

        if index is None:
            index = _default_index(N)

        if columns is None:
            columns = _default_index(K)

        return index, columns, values

    @property
    def _constructor(self):
        return DataMatrix

    def __array__(self):
        return self.values

    def __array_wrap__(self, result):
        return DataMatrix(result, index=self.index, columns=self.columns)

#-------------------------------------------------------------------------------
# DataMatrix-specific implementation of private API

    def _join_on(self, other, on):
        if len(other.index) == 0:
            return self

        if on not in self:
            raise Exception('%s column not contained in this frame!' % on)

        fillVec, mask = tseries.getMergeVec(self[on],
                                            other.index.indexMap)
        notmask = -mask

        tmpMatrix = other.values.take(fillVec, axis=0)
        tmpMatrix[notmask] = NaN

        seriesDict = dict((col, tmpMatrix[:, j])
                           for j, col in enumerate(other.columns))

        if getattr(other, 'objects'):
            objects = other.objects

            tmpMat = objects.values.take(fillVec, axis=0)
            tmpMat[notmask] = NaN
            objDict = dict((col, tmpMat[:, j])
                           for j, col in enumerate(objects.columns))

            seriesDict.update(objDict)

        filledFrame = DataFrame(data=seriesDict, index=self.index)

        return self.join(filledFrame, how='left')

    def _reindex_index(self, index, method):
        if index is self.index:
            return self.copy()

        if len(self.index) == 0:
            return DataMatrix(index=index, columns=self.columns)

        indexer, mask = common.get_indexer(self.index, index, method)
        mat = self.values.take(indexer, axis=0)

        notmask = -mask
        if len(index) > 0:
            if notmask.any():
                if issubclass(mat.dtype.type, np.int_):
                    mat = mat.astype(float)
                elif issubclass(mat.dtype.type, np.bool_):
                    mat = mat.astype(float)

                common.null_out_axis(mat, notmask, 0)

        if self.objects is not None and len(self.objects.columns) > 0:
            newObjects = self.objects.reindex(index)
        else:
            newObjects = None

        return DataMatrix(mat, index=index, columns=self.columns,
                          objects=newObjects)

    def _reindex_columns(self, columns):
        if len(columns) == 0:
            return DataMatrix(index=self.index)

        if self.objects is not None:
            object_columns = columns.intersection(self.objects.columns)
            columns = columns - object_columns

            objects = self.objects._reindex_columns(object_columns)
        else:
            objects = None

        if len(columns) > 0 and len(self.columns) == 0:
            return DataMatrix(index=self.index, columns=columns,
                              objects=objects)

        indexer, mask = self.columns.get_indexer(columns)
        mat = self.values.take(indexer, axis=1)

        notmask = -mask
        if len(mask) > 0:
            if notmask.any():
                if issubclass(mat.dtype.type, np.int_):
                    mat = mat.astype(float)
                elif issubclass(mat.dtype.type, np.bool_):
                    mat = mat.astype(float)

                common.null_out_axis(mat, notmask, 1)

        return DataMatrix(mat, index=self.index, columns=columns,
                          objects=objects)

    def _rename_columns_inplace(self, mapper):
        self.columns = [mapper(x) for x in self.columns]

        if self.objects is not None:
            self.objects._rename_columns_inplace(mapper)

    def _combine_frame(self, other, func):
        """
        Methodology, briefly
        - Really concerned here about speed, space

        - Get new index
        - Reindex to new index
        - Determine new_columns and commonColumns
        - Add common columns over all (new) indices
        - Fill to new set of columns

        Could probably deal with some Cython action in here at some point
        """
        new_index = self._union_index(other)

        if not self and not other:
            return DataMatrix(index=new_index)
        elif not self:
            return other * NaN
        elif not other:
            return self * NaN

        need_reindex = False
        new_columns = self._union_columns(other)
        need_reindex = (need_reindex or new_index is not self.index
                        or new_index is not other.index)
        need_reindex = (need_reindex or new_columns is not self.columns
                        or new_columns is not other.columns)

        this = self
        if need_reindex:
            this = self.reindex(index=new_index, columns=new_columns)
            other = other.reindex(index=new_index, columns=new_columns)

        return DataMatrix(func(this.values, other.values),
                          index=new_index, columns=new_columns)

    def _combine_match_index(self, other, func):
        new_index = self._union_index(other)
        values = self.values
        other_vals = other.values

        # Operate row-wise
        if not other.index.equals(new_index):
            other_vals = other.reindex(new_index).values

        if not self.index.equals(new_index):
            values = self.reindex(new_index).values

        return DataMatrix(func(values.T, other_vals).T,
                          index=new_index, columns=self.columns)

    def _combine_match_columns(self, other, func):
        newCols = self.columns.union(other.index)

        # Operate column-wise
        this = self.reindex(columns=newCols)
        other = other.reindex(newCols).values

        return DataMatrix(func(this.values, other),
                          index=self.index, columns=newCols)

    def _combine_const(self, other, func):
        if not self:
            return self

        # TODO: deal with objects
        return DataMatrix(func(self.values, other), index=self.index,
                          columns=self.columns)


#-------------------------------------------------------------------------------
# Properties for index and columns

    def _set_columns(self, cols):
        if len(cols) != self.values.shape[1]:
            raise Exception('Columns length %d did not match values %d!' %
                            (len(cols), self.values.shape[1]))

        self._columns = _ensure_index(cols)

    def _set_index(self, index):
        if len(index) > 0:
            if len(index) != self.values.shape[0]:
                raise Exception('Index length %d did not match values %d!' %
                                (len(index), self.values.shape[0]))

        self._index = _ensure_index(index)

        if self.objects is not None:
            self.objects._index = self._index

#-------------------------------------------------------------------------------
# "Magic methods"

    def __getstate__(self):
        if self.objects is not None:
            objects = self.objects._matrix_state(pickle_index=False)
        else:
            objects = None

        state = self._matrix_state()

        return (state, objects)

    def _matrix_state(self, pickle_index=True):
        columns = _pickle_array(self.columns)

        if pickle_index:
            index = _pickle_array(self.index)
        else:
            index = None

        return self.values, index, columns

    def __setstate__(self, state):
        (vals, idx, cols), object_state = state

        self.values = vals
        self.index = _unpickle_array(idx)
        self.columns = _unpickle_array(cols)

        if object_state:
            ovals, _, ocols = object_state
            self.objects = DataMatrix(ovals,
                                      index=self.index,
                                      columns=_unpickle_array(ocols))
        else:
            self.objects = None

    def __nonzero__(self):
        N, K = self.values.shape
        if N == 0 or K == 0:
            if self.objects is None:
                return False
            else:
                return self.objects.__nonzero__()
        else:
            return True

    def __getitem__(self, item):
        """
        Retrieve column, slice, or subset from DataMatrix.

        Possible inputs
        ---------------
        single value : retrieve a column as a Series
        slice : reindex to indices specified by slice
        boolean vector : like slice but more general, reindex to indices
          where the input vector is True

        Examples
        --------
        column = dm['A']

        dmSlice = dm[:20] # First 20 rows

        dmSelect = dm[dm.count(axis=1) > 10]

        Notes
        -----
        This is a magic method. Do NOT call explicity.
        """
        if isinstance(item, slice):
            new_index = self.index[item]
            new_values = self.values[item].copy()

            if self.objects is not None:
                new_objects = self.objects.reindex(new_index)
            else:
                new_objects = None

            return DataMatrix(new_values, index=new_index,
                              columns=self.columns,
                              objects=new_objects)

        elif isinstance(item, np.ndarray):
            if len(item) != len(self.index):
                raise Exception('Item wrong length %d instead of %d!' %
                                (len(item), len(self.index)))
            new_index = self.index[item]
            return self.reindex(new_index)
        else:
            values = self._data.get(item)
            return Series(values, index=self.index)

    # __setitem__ logic

    def _boolean_set(self, key, value):
        mask = key.values
        if mask.dtype != np.bool_:
            raise Exception('Must pass DataFrame with boolean values only')

        self.values[mask] = value

    def _insert_item(self, key, value):
        """
        Add series to DataMatrix in specified column.

        If series is a numpy-array (not a Series/TimeSeries), it must be the
        same length as the DataMatrix's index or an error will be thrown.

        Series/TimeSeries will be conformed to the DataMatrix's index to
        ensure homogeneity.
        """
        if hasattr(value, '__iter__'):
            if isinstance(value, Series):
                if value.index.equals(self.index):
                    # no need to copy
                    value = value.values
                else:
                    value = value.reindex(self.index).values
            else:
                assert(len(value) == len(self.index))

                if not isinstance(value, np.ndarray):
                    value = np.array(value)
                    if value.dtype.type == np.str_:
                        value = np.array(value, dtype=object)
        else:
            value = np.repeat(value, len(self.index))

        if self.values.dtype == np.object_:
            self._insert_object_dtype(key, value)
        else:
            self._insert_float_dtype(key, value)

    _dataTypes = [np.float_, np.bool_, np.int_]
    def _insert_float_dtype(self, key, value):
        isObject = value.dtype not in self._dataTypes

        # sanity check
        if len(value) != len(self.index): # pragma: no cover
            raise Exception('Column is wrong length')

        def _put_object(value):
            if self.objects is None:
                self.objects = DataMatrix({key : value},
                                          index=self.index)
            else:
                self.objects[key] = value

        if key in self.columns:
            loc = self.columns.indexMap[key]
            try:
                # attempt coercion
                self.values[:, loc] = value
            except ValueError:
                self._delete_column(loc)
                self._delete_column_index(loc)
                _put_object(value)
        elif isObject:
            _put_object(value)
        else:
            loc = self._get_insert_loc(key)
            self._insert_column(value.astype(float), loc)
            self._insert_column_index(key, loc)

    def _insert_object_dtype(self, key, value):
        if key in self.columns:
            loc = self.columns.indexMap[key]
            self.values[:, loc] = value
        else:
            loc = self._get_insert_loc(key)
            self._insert_column(value, loc)
            self._insert_column_index(key, loc)

    def __delitem__(self, key):
        """
        Delete column from DataMatrix
        """
        if key in self.columns:
            loc = self.columns.indexMap[key]
            self._delete_column(loc)
            self._delete_column_index(loc)
        else:
            if self.objects is not None and key in self.objects:
                del self.objects[key]
            else:
                raise KeyError('%s' % key)

    def _insert_column(self, column, loc):
        mat = self.values

        if column.ndim == 1:
            column = column.reshape((len(column), 1))

        if loc == mat.shape[1]:
            values = np.hstack((mat, column))
        elif loc == 0:
            values = np.hstack((column, mat))
        else:
            values = np.hstack((mat[:, :loc], column, mat[:, loc:]))

        self._float_values = values

    def _delete_column(self, loc):
        values = self._float_values

        if loc == values.shape[1] - 1:
            new_values = values[:, :loc]
        else:
            new_values = np.c_[values[:, :loc], values[:, loc+1:]]

        self._float_values = new_values

    def __iter__(self):
        """Iterate over columns of the frame."""
        return iter(self.columns)

    def __contains__(self, key):
        """True if DataMatrix has this column"""
        hasCol = key in self.columns
        if hasCol:
            return True
        else:
            if self.objects is not None and key in self.objects:
                return True
            return False

    def iteritems(self):
        return self._series.iteritems()

#-------------------------------------------------------------------------------
# Helper methods

    # For DataFrame compatibility
    def _getSeries(self, item=None, loc=None):
        if loc is None:
            try:
                loc = self.columns.indexMap[item]
            except KeyError:
                raise Exception('%s not here!' % item)
        return Series(self.values[:, loc], index=self.index)

    # to support old APIs
    def _series(self):
        return self._data.get_series_dict(self.index)

    def _output_columns(self):
        # for toString
        cols = list(self.columns)
        if self.objects is None:
            return cols
        else:
            return cols + list(self.objects.columns)

#-------------------------------------------------------------------------------
# Public methods

    def apply(self, func, axis=0, broadcast=False):
        """
        Applies func to columns (Series) of this DataMatrix and returns either
        a DataMatrix (if the function produces another series) or a Series
        indexed on the column names of the DataFrame if the function produces
        a value.

        Parameters
        ----------
        func : function
            Function to apply to each column
        broadcast : bool, default False
            For aggregation functions, return object of same size with values
            propagated

        Examples
        --------

            >>> df.apply(numpy.sqrt) --> DataMatrix
            >>> df.apply(numpy.sum) --> Series

        N.B.: Do NOT use functions that might toy with the index.
        """
        if not len(self.cols()):
            return self

        if isinstance(func, np.ufunc):
            results = func(self.values)
            return DataMatrix(data=results, index=self.index,
                              columns=self.columns, objects=self.objects)
        else:
            return DataFrame.apply(self, func, axis=axis,
                                   broadcast=broadcast)

    def applymap(self, func):
        """
        Apply a function to a DataMatrix that is intended to operate
        elementwise, i.e. like doing
            map(func, series) for each series in the DataMatrix

        Parameters
        ----------
        func : function
            Python function, returns a single value from a single value

        Note : try to avoid using this function if you can, very slow.
        """
        npfunc = np.frompyfunc(func, 1, 1)
        results = npfunc(self.values)
        try:
            results = results.astype(self.values.dtype)
        except Exception:
            pass

        return DataMatrix(results, index=self.index, columns=self.columns)

    def append(self, other):
        """
        Glue together DataFrame objects having non-overlapping indices

        Parameters
        ----------
        other : DataFrame
        """
        if not other:
            return self.copy()

        if not self:
            return other.copy()

        if (isinstance(other, DataMatrix) and
            self.columns.equals(other.columns)):

            idx = Index(np.concatenate([self.index, other.index]))
            mat = np.vstack((self.values, other.values))

            if other.objects is None:
                objects = self.objects
            elif self.objects is None:
                objects = other.objects
            else:
                objects = self.objects.append(other.objects)

            if objects:
                objects = objects.reindex(idx)

            dm = DataMatrix(mat, idx, self.columns, objects=objects)
            return dm
        else:
            return super(DataMatrix, self).append(other)

    def asMatrix(self, columns=None):
        """
        Convert the DataMatrix to its Numpy-array matrix representation

        Columns are presented in sorted order unless a specific list
        of columns is provided.

        Parameters
        ----------
        columns : list-like
            columns to use in producing matrix, must all be contained

        Returns
        -------
        ndarray
        """
        if columns is None:
            values = self.values.copy()

            if self.objects:
                values = np.column_stack((values, self.objects.values))
                order = Index(np.concatenate((self.columns,
                                                self.objects.columns)))
            else:
                order = self.columns

            columns = Index(self.cols())
        else:
            columns = _ensure_index(columns)
            values = self.values
            order = self.columns

            if self.objects:
                idxMap = self.objects.columns.indexMap
                indexer = [idxMap[col] for col in columns if col in idxMap]

                obj_values = self.objects.values.take(indexer, axis=1)

                values = np.column_stack((values, obj_values))

                order = Index(np.concatenate((order, self.objects.columns)))

        # now put in the right order
        return _reorder_columns(values, order, columns)

    def cols(self):
        """Return sorted list of frame's columns"""
        if self.objects is not None and len(self.objects.columns) > 0:
            return list(self.columns.union(self.objects.columns))
        else:
            return list(self.columns)

    def copy(self):
        """
        Make a copy of this DataMatrix
        """
        if self.objects:
            objects = self.objects.copy()
        else:
            objects = None

        return DataMatrix(self.values.copy(), index=self.index,
                          columns=self.columns, objects=objects)

    def cumsum(self, axis=0):
        """
        Return DataMatrix of cumulative sums over requested axis.

        Parameters
        ----------
        axis : {0, 1}
            0 for row-wise, 1 for column-wise

        Returns
        -------
        y : DataMatrix
        """
        y = np.array(self.values, subok=True)
        if not issubclass(y.dtype.type, np.int_):
            mask = np.isnan(self.values)
            y[mask] = 0
            result = y.cumsum(axis)
            has_obs = (-mask).astype(int).cumsum(axis) > 0
            result[-has_obs] = np.NaN
        else:
            result = y.cumsum(axis)

        return DataMatrix(result, index=self.index,
                          columns=self.columns, objects=self.objects)

    def min(self, axis=0):
        """
        Return array or Series of minimums over requested axis.

        Parameters
        ----------
        axis : {0, 1}
            0 for row-wise, 1 for column-wise

        Returns
        -------
        Series or TimeSeries
        """
        values = self.values.copy()
        np.putmask(values, -np.isfinite(values), np.inf)
        return Series(values.min(axis), index=self._get_agg_axis(axis))

    def max(self, axis=0):
        """
        Return array or Series of maximums over requested axis.

        Parameters
        ----------
        axis : {0, 1}
            0 for row-wise, 1 for column-wise

        Returns
        -------
        Series or TimeSeries
        """
        values = self.values.copy()
        np.putmask(values, -np.isfinite(values), -np.inf)
        return Series(values.max(axis), index=self._get_agg_axis(axis))

    def fillna(self, value=None, method='pad'):
        """
        Fill NaN values using the specified method.

        Member Series / TimeSeries are filled separately.

        Parameters
        ----------
        value : any kind (should be same type as array)
            Value to use to fill holes (e.g. 0)

        method : {'backfill', 'pad', None}
            Method to use for filling holes in new inde

        Returns
        -------
        y : DataMatrix

        See also
        --------
        DataMatrix.reindex, DataMatrix.asfreq
        """
        if value is None:
            result = {}
            series = self._series
            for col, s in series.iteritems():
                result[col] = s.fillna(method=method, value=value)

            return DataMatrix(result, index=self.index, objects=self.objects)
        else:
            # Float type values
            if len(self.columns) == 0:
                return self

            vals = self.values.copy()
            vals.flat[common.isnull(vals.ravel())] = value

            objects = None

            if self.objects is not None:
                objects = self.objects.copy()

            return DataMatrix(vals, index=self.index, columns=self.columns,
                              objects=objects)

    def xs(self, key, copy=True):
        """
        Returns a row from the DataMatrix as a Series object.

        Parameters
        ----------
        key : some index contained in the index

        Returns
        -------
        Series
        """
        if key not in self.index:
            raise Exception('No cross-section for %s' % key)

        loc = self.index.indexMap[key]
        xs = self.values[loc, :]

        if copy:
            xs = xs.copy()
        result = Series(xs, index=self.columns)

        if self.objects is not None and len(self.objects.columns) > 0:
            if not copy:
                raise Exception('cannot get view of mixed-type cross-section')
            result = result.append(self.objects.xs(key))

        return result

    @property
    def T(self):
        """
        Returns a DataMatrix with the rows/columns switched.
        """
        if self.objects is not None:
            objectsT = self.objects.values.T
            valuesT = self.values.T
            new_values = np.concatenate((valuesT, objectsT), axis=0)
            new_index = Index(np.concatenate((self.columns,
                                              self.objects.columns)))

            return DataMatrix(new_values, index=new_index, columns=self.index)
        else:
            return DataMatrix(data=self.values.T, index=self.columns,
                              columns=self.index)

    def shift(self, periods, offset=None, timeRule=None):
        """
        Shift the underlying series of the DataMatrix and Series objects within
        by given number (positive or negative) of periods.

        Parameters
        ----------
        periods : int (+ or -)
            Number of periods to move
        offset : DateOffset, optional
            Increment to use from datetools module
        timeRule : string
            Time rule to use by name

        Returns
        -------
        DataMatrix
        """
        if periods == 0:
            return self

        if timeRule is not None and offset is None:
            offset = datetools.getOffset(timeRule)

        if offset is None:
            indexer = self._shift_indexer(periods)
            new_values = self.values.take(indexer, axis=0)
            new_index = self.index

            new_values = common.ensure_float(new_values)

            if periods > 0:
                new_values[:periods] = NaN
            else:
                new_values[periods:] = NaN
        else:
            new_index = self.index.shift(periods, offset)
            new_values = self.values.copy()

        if self.objects is not None:
            shifted_objects = self.objects.shift(periods, offset=offset,
                                                 timeRule=timeRule)

            shifted_objects.index = new_index
        else:
            shifted_objects = None

        return DataMatrix(data=new_values, index=new_index,
                          columns=self.columns, objects=shifted_objects)

_data_types = [np.float_, np.int_]

def _filter_out(data, columns):
    if columns is not None:
        colset = set(columns)
        data = dict((k, v) for k, v in data.iteritems() if k in colset)

    return data


def _group_dtypes(data, columns):
    import itertools

    chunk_cols = []
    chunks = []
    for dtype, gp_cols in itertools.groupby(columns, lambda x: data[x].dtype):
        chunk = np.vstack([data[k] for k in gp_cols]).T

        chunks.append(chunk)
        chunk_cols.append(gp_cols)

    return chunks, chunk_cols

def _init_dict(data, index, columns):
    """
    Segregate Series based on type and coerce into matrices.

    Needs to handle a lot of exceptional cases.

    Somehow this got outrageously complicated
    """
    # pre-filter out columns if we passed it
    if columns is None:
        columns = _try_sort(data.keys())
    columns = _ensure_index(columns)

    # prefilter
    data = dict((k, v) for k, v in data.iteritems() if k in columns)

    # figure out the index, if necessary
    if index is None:
        index = extract_index(data)
    homogenized = _homogenize_series(data, index)
    # segregates dtypes and forms blocks
    blocks = _segregate_dtypes(data)

    if columns is None:
        columns = Index(_try_sort(valueDict))
        objectColumns = Index(_try_sort(objectDict))
    else:
        objectColumns = Index([c for c in columns if c in objectDict])
        columns = Index([c for c in columns if c not in objectDict])

    values = np.empty((len(index), len(columns)), dtype=dtype)

    for i, col in enumerate(columns):
        if col in valueDict:
            values[:, i] = valueDict[col].values
        else:
            values[:, i] = np.NaN

    return index, columns, values, objects

def _homogenize_series(data, index):
    homogenized = {}

    for k, v in data.iteritems():
        if isinstance(v, Series):
            if v.index is not index:
                # Forces alignment. No need to copy data since we
                # are putting it into an ndarray later
                v = v.reindex(index)
        else:
            if isinstance(v, dict):
                v = [v.get(i, NaN) for i in index]
            else:
                assert(len(v) == len(index))
            v = Series(v, index=index)

        if issubclass(v.dtype.type, (float, int)):
            v = v.astype(np.float64)
        else:
            v = v.astype(object)

        homogenized[k] = v

    return homogenized

def _segregate_dtypes(data):
    float_dict = {}
    object_dict = {}
    for k, v in data.iteritems():
        if issubclass(v.dtype.type, (np.floating, np.integer)):
            float_dict[k] = v
        else:
            object_dict[k] = v

    float_block = _blockify(float_dict, np.float64)
    object_block = _blockify(object_dict, np.object_)
    return [float_block, object_block]

def _blockify(dct, dtype):
    pass

def _init_matrix(self, values, index, columns, dtype):
    if not isinstance(values, np.ndarray):
        arr = np.array(values)
        if issubclass(arr.dtype.type, basestring):
            arr = np.array(values, dtype=object, copy=True)

        values = arr

    if values.ndim == 1:
        N = values.shape[0]
        if N == 0:
            values = values.reshape((values.shape[0], 0))
        else:
            values = values.reshape((values.shape[0], 1))

    if dtype is not None:
        try:
            values = values.astype(dtype)
        except Exception:
            pass

    N, K = values.shape

    if index is None:
        index = _default_index(N)

    if columns is None:
        columns = _default_index(K)

    return index, columns, values

def _reorder_columns(mat, current, desired):
    indexer, mask = common.get_indexer(current, desired, None)
    return mat.take(indexer[mask], axis=1)

if __name__ == '__main__':
    pass
