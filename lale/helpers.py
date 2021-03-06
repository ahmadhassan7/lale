# Copyright 2019 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import numpy as np
import pandas as pd
import os
import re
import sys
import time
import traceback
import scipy.sparse
import importlib
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import accuracy_score, log_loss
from sklearn.metrics.scorer import check_scoring
from sklearn.utils.metaestimators import _safe_split
import copy
import logging
import h5py
from typing import Any, Dict, List, Optional, Union
import lale.datasets.data_schemas

try:
    import torch
    torch_installed=True
except ImportError:
    torch_installed=False

logger = logging.getLogger(__name__)

class NestedHyperoptSpace():
    sub_space:Any
    def __init__(self, sub_space):
        self.sub_space = sub_space
    def __str__(self):
        return str(self.sub_space)
    #define __repr__ so that __str__ gets invoked while printing lists and dictionaries
    __repr__ = __str__

def assignee_name(level=1) -> Optional[str]:
    tb = traceback.extract_stack()
    file_name, line_number, function_name, text = tb[-(level+2)]
    tree = ast.parse(text, file_name)
    assert isinstance(tree, ast.Module)
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Assign):
        lhs = tree.body[0].targets
        if len(lhs) == 1 and isinstance(lhs[0], ast.Name):
            return lhs[0].id
    return None

def data_to_json(data, subsample_array:bool=True) -> Union[list, dict]:
    if type(data) is tuple:
        # convert to list
        return [data_to_json(elem, subsample_array) for elem in data]
    if type(data) is list:
        return [data_to_json(elem, subsample_array) for elem in data]
    elif type(data) is dict:
        return {key: data_to_json(data[key], subsample_array) for key in data}
    elif isinstance(data, np.ndarray):
        return ndarray_to_json(data, subsample_array)
    elif type(data) is scipy.sparse.csr_matrix:
        return ndarray_to_json(data.toarray(), subsample_array)
    elif isinstance(data, pd.DataFrame) or isinstance(data, pd.Series):
        np_array = data.values
        return ndarray_to_json(np_array, subsample_array)
    elif torch_installed and isinstance(data, torch.Tensor):
        np_array = data.detach().numpy()
        return ndarray_to_json(np_array, subsample_array)
    else:
        return data

def is_empty_dict(val) -> bool:
    return isinstance(val, dict) and len(val) == 0

def dict_without(orig_dict: Dict[str, Any], key: str) -> Dict[str, Any]:
    return {k: orig_dict[k] for k in orig_dict if k != key}

def ndarray_to_json(arr, subsample_array:bool=True) -> Union[list, dict]:
    #sample 10 rows and no limit on columns
    if subsample_array:
        num_subsamples = [10, np.iinfo(np.int).max, np.iinfo(np.int).max]
    else:
        num_subsamples = [np.iinfo(np.int).max, np.iinfo(np.int).max, np.iinfo(np.int).max]
    def subarray_to_json(indices):
        if len(indices) == len(arr.shape):
            if isinstance(arr[indices], bool) or \
               isinstance(arr[indices], int) or \
               isinstance(arr[indices], float) or \
               isinstance(arr[indices], str):
                return arr[indices]
            elif np.issubdtype(arr.dtype, np.bool_):
                return bool(arr[indices])
            elif np.issubdtype(arr.dtype, np.integer):
                return int(arr[indices])
            elif np.issubdtype(arr.dtype, np.number):
                return float(arr[indices])
            elif arr.dtype.kind in ['U', 'S', 'O']:
                return str(arr[indices])
            else:
                raise ValueError(f'Unexpected dtype {arr.dtype}, '
                                 f'kind {arr.dtype.kind}, '
                                 f'type {type(arr[indices])}.')
        else:
            assert len(indices) < len(arr.shape)
            return [subarray_to_json(indices + (i,))
                    for i in range(min(num_subsamples[len(indices)], arr.shape[len(indices)]))]
    return subarray_to_json(())

def split_with_schemas(estimator, all_X, all_y, indices, train_indices=None):
    subset_X, subset_y = _safe_split(
        estimator, all_X, all_y, indices, train_indices)
    if hasattr(all_X, 'json_schema'):
        n_rows = subset_X.shape[0]
        schema = {
            'type': 'array', 'minItems': n_rows, 'maxItems': n_rows,
            'items': all_X.json_schema['items']}
        lale.datasets.data_schemas.add_schema(subset_X, schema)
    if hasattr(all_y, 'json_schema'):
        n_rows = subset_y.shape[0]
        schema = {
            'type': 'array', 'minItems': n_rows, 'maxItems': n_rows,
            'items': all_y.json_schema['items']}
        lale.datasets.data_schemas.add_schema(subset_y, schema)
    return subset_X, subset_y

def cross_val_score_track_trials(estimator, X, y=None, scoring=accuracy_score, cv=5, args_to_scorer=None):
    """
    Use the given estimator to perform fit and predict for splits defined by 'cv' and compute the given score on 
    each of the splits.

    Parameters
    ----------

    estimator: A valid sklearn_wrapper estimator
    X, y: Valid data and target values that work with the estimator
    scoring: string or a scorer object created using 
        https://scikit-learn.org/stable/modules/generated/sklearn.metrics.make_scorer.html#sklearn.metrics.make_scorer.
        A string from sklearn.metrics.SCORERS.keys() can be used or a scorer created from one of 
        sklearn.metrics (https://scikit-learn.org/stable/modules/classes.html#module-sklearn.metrics).
        A completely custom scorer object can be created from a python function following the example at 
        https://scikit-learn.org/stable/modules/model_evaluation.html
        The metric has to return a scalar value,
    cv: an integer or an object that has a split function as a generator yielding (train, test) splits as arrays of indices.
        Integer value is used as number of folds in sklearn.model_selection.StratifiedKFold, default is 5.
        Note that any of the iterators from https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators can be used here.
    args_to_scorer: A dictionary of additional keyword arguments to pass to the scorer. 
                Used for cases where the scorer has a signature such as ``scorer(estimator, X, y, **kwargs)``.
    Returns
    -------
        cv_results: a list of scores corresponding to each cross validation fold
    """
    if isinstance(cv, int):
        cv = StratifiedKFold(cv)

    if args_to_scorer is None:
        args_to_scorer={}
    scorer = check_scoring(estimator, scoring=scoring)
    cv_results:List[float] = []
    log_loss_results = []
    time_results = []
    for train, test in cv.split(X, y):
        X_train, y_train = split_with_schemas(estimator, X, y, train)
        X_test, y_test = split_with_schemas(estimator, X, y, test, train)
        start = time.time()
        #Not calling sklearn.base.clone() here, because:
        #  (1) For Lale pipelines, clone() calls the pipeline constructor
        #      with edges=None, so the resulting topology is incorrect.
        #  (2) For Lale individual operators, the fit() method already
        #      clones the impl object, so cloning again is redundant.
        trained = estimator.fit(X_train, y_train)
        score_value  = scorer(trained, X_test, y_test, **args_to_scorer)
        execution_time = time.time() - start
        # not all estimators have predict probability
        try:
            y_pred_proba = trained.predict_proba(X_test)
            logloss = log_loss(y_true=y_test, y_pred=y_pred_proba)
            log_loss_results.append(logloss)
        except BaseException:
            logger.debug("Warning, log loss cannot be computed")
        cv_results.append(score_value)
        time_results.append(execution_time)
    result = np.array(cv_results).mean(), np.array(log_loss_results).mean(), np.array(execution_time).mean()
    return result


def cross_val_score(estimator, X, y=None, scoring=accuracy_score, cv=5):
    """
    Use the given estimator to perform fit and predict for splits defined by 'cv' and compute the given score on
    each of the splits.
    :param estimator: A valid sklearn_wrapper estimator
    :param X, y: Valid data and target values that work with the estimator
    :param scoring: a scorer object from sklearn.metrics (https://scikit-learn.org/stable/modules/classes.html#module-sklearn.metrics)
             Default value is accuracy_score.
    :param cv: an integer or an object that has a split function as a generator yielding (train, test) splits as arrays of indices.
        Integer value is used as number of folds in sklearn.model_selection.StratifiedKFold, default is 5.
        Note that any of the iterators from https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators can be used here.
    :return: cv_results: a list of scores corresponding to each cross validation fold
    """
    if isinstance(cv, int):
        cv = StratifiedKFold(cv)

    cv_results = []
    for train, test in cv.split(X, y):
        X_train, y_train = split_with_schemas(estimator, X, y, train)
        X_test, y_test = split_with_schemas(estimator, X, y, test, train)
        trained_estimator = estimator.fit(X_train, y_train)
        predicted_values = trained_estimator.predict(X_test)
        cv_results.append(scoring(y_test, predicted_values))

    return cv_results

def create_operator_using_reflection(class_name, operator_name, param_dict):
    instance = None
    if class_name is not None:
        class_name_parts = class_name.split(".")
        assert(len(class_name_parts)) >1, "The class name needs to be fully qualified, i.e. module name + class name"
        module_name = ".".join(class_name_parts[0:-1])
        class_name = class_name_parts[-1]

        module = importlib.import_module(module_name)

        class_ = getattr(module, class_name)
        
        if param_dict is None:
            instance = class_.create(name=operator_name)
        else:
            instance = class_.create(name=operator_name, kwargs = param_dict)
    return instance

def create_individual_op_using_reflection(class_name, operator_name, param_dict):
    instance = None
    if class_name is not None:
        class_name_parts = class_name.split(".")
        assert(len(class_name_parts)) >1, "The class name needs to be fully qualified, i.e. module name + class name"
        module_name = ".".join(class_name_parts[0:-1])
        class_name = class_name_parts[-1]

        module = importlib.import_module(module_name)
        class_ = getattr(module, class_name)
        
        if param_dict is None:
            instance = class_()
        else:
            instance = class_(**param_dict)
    return instance

def to_graphviz(lale_operator: 'lale.operators.Operator', ipython_display:bool=True, call_depth:int=1, **dot_graph_attr):
    import lale.visualize
    if not isinstance(lale_operator, lale.operators.Operator):
        raise TypeError("The input to to_graphviz needs to be a valid LALE operator.")
    jsn = lale.json_operator.to_json(lale_operator, call_depth=call_depth+1)
    dot = lale.visualize.json_to_graphviz(jsn, ipython_display, dot_graph_attr)
    return dot

def println_pos(message, out_file=sys.stdout):
    tb = traceback.extract_stack()[-2]
    match = re.search(r'<ipython-input-([0-9]+)-', tb[0])
    if match:
        pos = 'notebook cell [{}] line {}'.format(match[1], tb[1])
    else:
        pos = '{}:{}'.format(tb[0], tb[1])
    strtime = time.strftime('%Y-%m-%d_%H-%M-%S')
    to_log = '{}: {} {}'.format(pos, strtime, message)
    print(to_log, file=out_file)
    if match:
        os.system('echo {}'.format(to_log))

def instantiate_from_hyperopt_search_space(obj_hyperparams, new_hyperparams):
    if isinstance(new_hyperparams, NestedHyperoptSpace):
        sub_params = new_hyperparams.sub_space

        sub_op = obj_hyperparams
        if isinstance(sub_op, list):
            if len(sub_op)==1:
                sub_op = sub_op[0]
            else:
                step_index, step_params = list(sub_params)[0]
                if step_index < len(sub_op):
                    sub_op = sub_op[step_index]
                    sub_params = step_params

        return create_instance_from_hyperopt_search_space(sub_op, sub_params)

    elif isinstance(new_hyperparams, (list, tuple)):
        assert isinstance(obj_hyperparams, (list, tuple))
        params_len = len(new_hyperparams)
        assert params_len == len(obj_hyperparams)
        res:Optional[List[Any]] = None

        for i in range(params_len):
            nhi = new_hyperparams[i]
            ohi = obj_hyperparams[i]
            updated_params = instantiate_from_hyperopt_search_space(ohi, nhi)
            if updated_params is not None:
                if res is None:
                    res = list(new_hyperparams)
                res[i] = updated_params
        if res is not None:
            if isinstance(obj_hyperparams, tuple):
                return tuple(res)
            else:
                return res
        # workaround for what seems to be a hyperopt bug
        # where hyperopt returns a tuple even though the
        # hyperopt search space specifies a list
        is_obj_tuple = isinstance(obj_hyperparams, tuple)
        is_new_tuple = isinstance(new_hyperparams, tuple)
        if is_obj_tuple != is_new_tuple:
            if is_obj_tuple:
                return tuple(new_hyperparams)
            else:
                return list(new_hyperparams)
        return None

    elif isinstance(new_hyperparams, dict):
        assert isinstance(obj_hyperparams, dict)

        for k,sub_params in new_hyperparams.items():
            if k in obj_hyperparams:
                sub_op = obj_hyperparams[k]
                updated_params = instantiate_from_hyperopt_search_space(sub_op, sub_params)
                if updated_params is not None:
                    new_hyperparams[k] = updated_params
        return None
    else:
        return None

def create_instance_from_hyperopt_search_space(lale_object, hyperparams):
    '''
    Hyperparams is a n-tuple of dictionaries of hyper-parameters, each
    dictionary corresponds to an operator in the pipeline
    '''
    #lale_object can either be an individual operator, a pipeline or an operatorchoice
    #Validate that the number of elements in the n-tuple is the same
    #as the number of steps in the current pipeline

    from lale.operators import IndividualOp
    from lale.operators import BasePipeline
    from lale.operators import TrainablePipeline
    from lale.operators import Operator
    from lale.operators import OperatorChoice
    if isinstance(lale_object, IndividualOp):
        new_hyperparams:Dict[str,Any] = dict_without(hyperparams, 'name')
        if lale_object._hyperparams is not None:
            obj_hyperparams = dict(lale_object._hyperparams)
        else:
            obj_hyperparams = {}

        for k,sub_params in new_hyperparams.items():
            if k in obj_hyperparams:
                sub_op = obj_hyperparams[k]
                updated_params = instantiate_from_hyperopt_search_space(sub_op, sub_params)
                if updated_params is not None:
                    new_hyperparams[k] = updated_params

        all_hyperparams = {**obj_hyperparams, **new_hyperparams}
        return lale_object(**all_hyperparams)
    elif isinstance(lale_object, BasePipeline):
        steps = lale_object.steps()
        if len(hyperparams) != len(steps):
            raise ValueError('The number of steps in the hyper-parameter space does not match the number of steps in the pipeline.')
        op_instances = []
        edges = lale_object.edges()
        #op_map:Dict[PlannedOpType, TrainableOperator] = {}
        op_map = {}
        for op_index, sub_params in enumerate(hyperparams):
            sub_op = steps[op_index]
            op_instance = create_instance_from_hyperopt_search_space(sub_op, sub_params)
            assert isinstance(sub_op, OperatorChoice) or sub_op.class_name() == op_instance.class_name(), f'sub_op {sub_op.class_name()}, op_instance {op_instance.class_name()}'
            op_instances.append(op_instance)
            op_map[sub_op] = op_instance

        #trainable_edges:List[Tuple[TrainableOperator, TrainableOperator]]
        try:
            trainable_edges = [(op_map[x], op_map[y]) for (x, y) in edges]
        except KeyError as e:
            raise ValueError("An edge was found with an endpoint that is not a step (" + str(e) + ")")

        return TrainablePipeline(op_instances, trainable_edges, ordered=True)
    elif isinstance(lale_object, OperatorChoice):
        #Hyperopt search space for an OperatorChoice is generated as a dictionary with a single element
        #corresponding to the choice made, the only key is the index of the step and the value is 
        #the params corresponding to that step.
        step_index:int
        choices = lale_object.steps()

        if len(choices)==1:
            step_index = 0
        else:
            step_index, hyperparams = list(hyperparams.items())[0]
        step_object = choices[step_index]
        return create_instance_from_hyperopt_search_space(step_object, hyperparams)
        
def import_from_sklearn_pipeline(sklearn_pipeline, fitted=True):
    #For all pipeline steps, identify equivalent lale wrappers if present,
    #if not, call make operator on sklearn classes and create a lale pipeline.

    def get_equivalent_lale_op(sklearn_obj, fitted):
        module_names = ["lale.lib.sklearn", "lale.lib.autoai_libs"]
        from lale.operators import make_operator, TrainedIndividualOp

        lale_wrapper_found = False
        class_name = sklearn_obj.__class__.__name__
        for module_name in module_names:
            module = importlib.import_module(module_name)
            try:
                class_ = getattr(module, class_name)
                lale_wrapper_found  = True
                break
            except AttributeError:
                continue
        else:
            class_ = make_operator(sklearn_obj, name=class_name)

        if not fitted:#If fitted is False, we do not want to return a Trained operator.
            lale_op = class_
        else:
            lale_op = TrainedIndividualOp(class_._name, class_._impl, class_._schemas)

        orig_hyperparams = sklearn_obj.get_params()
        higher_order = False
        for hp_name, hp_val in orig_hyperparams.items():
            higher_order = higher_order or hasattr(hp_val, 'get_params')
        if higher_order:
            hyperparams = {}
            for hp_name, hp_val in orig_hyperparams.items():
                if hasattr(hp_val, 'get_params'):
                    nested_op = get_equivalent_lale_op(hp_val, fitted)
                    hyperparams[hp_name] = nested_op
                else:
                    hyperparams[hp_name] = hp_val
        else:
            hyperparams = orig_hyperparams

        class_ = lale_op(**hyperparams)
        if lale_wrapper_found:
            wrapped_model = copy.deepcopy(sklearn_obj)
            class_._impl_instance()._wrapped_model = wrapped_model
        else:# If there is no lale wrapper, there is no _wrapped_model
            class_._impl = copy.deepcopy(sklearn_obj) 
        return class_

    from sklearn.pipeline import FeatureUnion, Pipeline
    from sklearn.base import BaseEstimator
    from lale.operators import make_pipeline, make_union
    if isinstance(sklearn_pipeline, Pipeline):
        nested_pipeline_steps = sklearn_pipeline.named_steps.values()
        nested_pipeline_lale_objects = [import_from_sklearn_pipeline(nested_pipeline_step, fitted=fitted) for nested_pipeline_step in nested_pipeline_steps]
        lale_op_obj = make_pipeline(*nested_pipeline_lale_objects)
    elif isinstance(sklearn_pipeline, FeatureUnion):
        transformer_list = sklearn_pipeline.transformer_list
        concat_predecessors = [import_from_sklearn_pipeline(transformer[1], fitted=fitted) for transformer in transformer_list]
        lale_op_obj = make_union(*concat_predecessors)
    else:
        lale_op_obj = get_equivalent_lale_op(sklearn_pipeline, fitted=fitted)
    return lale_op_obj

class val_wrapper():
    """This is used to wrap values that cause problems for hyper-optimizer backends
    lale will unwrap these when given them as the value of a hyper-parameter"""

    def __init__(self, base):
        self._base = base

    def unwrap_self(self):
        return self._base

    @classmethod
    def unwrap(cls, obj):
        if isinstance(obj, cls):
            return cls.unwrap(obj.unwrap_self())
        else:
            return obj

def append_batch(data, batch_data):
    if data is None:
        return batch_data
    elif isinstance(data, np.ndarray):
        if isinstance(batch_data, np.ndarray):
            if len(data.shape) == 1 and len(batch_data.shape) == 1:
                return np.concatenate([data, batch_data])
            else:
                return np.vstack((data, batch_data))
    elif isinstance(data, tuple):
        X, y = data
        if isinstance(batch_data, tuple):
            batch_X, batch_y = batch_data
            X = append_batch(X, batch_X)
            y = append_batch(y, batch_y)
            return X, y
    elif torch_installed and isinstance(data, torch.Tensor):
        if isinstance(batch_data, torch.Tensor):
            return torch.cat((data, batch_data))
    elif isinstance(data, h5py.File):
        if isinstance(batch_data, tuple):
            batch_X, batch_y = batch_data
            
    #TODO:Handle dataframes

def create_data_loader(X, y = None, batch_size = 1):
    from lale.util.numpy_to_torch_dataset import NumpyTorchDataset
    from lale.util.hdf5_to_torch_dataset import HDF5TorchDataset
    from torch.utils.data import DataLoader, TensorDataset
    from lale.util.batch_data_dictionary_dataset import BatchDataDict
    import torch
    if isinstance(X, pd.DataFrame):
        X = X.to_numpy()
        if isinstance(y, pd.Series):
            y = y.to_numpy()
    elif isinstance(X, scipy.sparse.csr.csr_matrix):
        #unfortunately, NumpyTorchDataset won't accept a subclass of np.ndarray
        X = X.toarray()
        if isinstance(y, lale.datasets.data_schemas.NDArrayWithSchema):
            y = y.view(np.ndarray)
        dataset = NumpyTorchDataset(X, y)
    elif isinstance(X, np.ndarray):
        #unfortunately, NumpyTorchDataset won't accept a subclass of np.ndarray
        if isinstance(X, lale.datasets.data_schemas.NDArrayWithSchema):
            X = X.view(np.ndarray)
        if isinstance(y, lale.datasets.data_schemas.NDArrayWithSchema):
            y = y.view(np.ndarray)
        dataset = NumpyTorchDataset(X, y)
    elif isinstance(X, str):#Assume that this is path to hdf5 file
        dataset = HDF5TorchDataset(X)
    elif isinstance(X, BatchDataDict):
        dataset = X
        def my_collate_fn(batch):
            return batch[0]#because BatchDataDict's get_item returns a batch, so no collate is required.
        return DataLoader(dataset, batch_size=1, collate_fn=my_collate_fn)
    elif isinstance(X, dict): #Assumed that it is data indexed by batch number
        return [X]
    elif isinstance(X, torch.Tensor) and y is not None:
        if isinstance(y, np.ndarray):
            y = torch.from_numpy(y)
        dataset = TensorDataset(X, y)
    elif isinstance(X, torch.Tensor):
        dataset = TensorDataset(X)
    else:
        raise TypeError("Can not create a data loader for a dataset with type {}".format(type(X)))
    return DataLoader(dataset, batch_size=batch_size)

def write_batch_output_to_file(file_obj, file_path, total_len, batch_idx, batch_X, batch_y, batch_out_X, batch_out_y):
    if file_obj is None and file_path is None:
        raise ValueError("Only one of the file object or file path can be None.")
    if file_obj is None:
        file_obj = h5py.File(file_path, 'w')
        #estimate the size of the dataset based on the first batch output size
        transform_ratio = int(len(batch_out_X)/len(batch_X))
        if len(batch_out_X.shape) == 1:
            h5_data_shape = (transform_ratio*total_len, )
        if len(batch_out_X.shape) == 2:
            h5_data_shape = (transform_ratio*total_len, batch_out_X.shape[1])
        elif len(batch_out_X.shape) == 3:
            h5_data_shape = (transform_ratio*total_len, batch_out_X.shape[1], batch_out_X.shape[2])
        dataset = file_obj.create_dataset(name='X', shape=h5_data_shape, chunks=True, compression="gzip")
        if batch_out_y is None and batch_y is not None:
            batch_out_y = batch_y
        if batch_out_y is not None:
            if len(batch_out_y.shape) == 1:
                h5_labels_shape = (transform_ratio*total_len, )
            elif len(batch_out_y.shape) == 2:
                h5_labels_shape = (transform_ratio*total_len, batch_out_y.shape[1])
            dataset = file_obj.create_dataset(name='y', shape=h5_labels_shape, chunks=True, compression="gzip")
    dataset = file_obj['X']
    dataset[batch_idx*len(batch_out_X):(batch_idx+1)*len(batch_out_X)] = batch_out_X
    if batch_out_y is not None or batch_y is not None:
        labels = file_obj['y']
        if batch_out_y is not None:
            labels[batch_idx*len(batch_out_y):(batch_idx+1)*len(batch_out_y)] = batch_out_y
        else:
            labels[batch_idx*len(batch_y):(batch_idx+1)*len(batch_y)] = batch_y
    return file_obj

def best_estimator(obj):
    """
    .. deprecated:: 0.3.3
       The best_estimator(obj) function has been replaced by the
       obj.get_pipeline() method.
    """
    return obj.get_pipeline()
