# ----------------------------------------------------------------------------
# Copyright (c) 2017-2018, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections

from sklearn.ensemble import IsolationForest
from sklearn.metrics import mean_squared_error, accuracy_score
from sklearn.feature_extraction import DictVectorizer
from sklearn.pipeline import Pipeline

import qiime2
import pandas as pd
import biom
import skbio

from .utilities import (split_optimize_classify, _visualize, _load_data,
                        _set_parameters_and_estimator,
                        _prepare_training_data, _disable_feature_selection,
                        nested_cross_validation, _fit_estimator,
                        _extract_features, _plot_accuracy,
                        _summarize_estimator)


defaults = {
    'test_size': 0.2,
    'step': 0.05,
    'cv': 5,
    'n_jobs': 1,
    'n_estimators': 100,
    'estimator_c': 'RandomForestClassifier',
    'estimator_r': 'RandomForestRegressor',
    'palette': 'sirocco',
    'missing_samples': 'error'
}


def classify_samples_from_dist(ctx, distance_matrix, metadata, k=1,
                               palette=defaults['palette']):
    ''' Returns knn classifier results from a distance matrix.'''
    distance_matrix = distance_matrix.view(skbio.DistanceMatrix)
    predictions = []
    metadata_series = metadata.to_series()
    for i, row in enumerate(distance_matrix):
        dists = []
        categories = []
        for j, dist in enumerate(row):
            if j == i:
                continue  # exclude self
            dists.append(dist)
            categories.append(metadata_series[distance_matrix.ids[j]])

        # k-long series of (category: dist) ordered small -> large
        nn_categories = pd.Series(dists, index=categories).nsmallest(k)
        counter = collections.Counter(nn_categories.index)
        max_counts = max(counter.values())
        # in order of closeness, pick a category that is or shares
        # max_counts
        for category in nn_categories.index:
            if counter[category] == max_counts:
                predictions.append(category)
                break

    predictions = pd.Series(predictions, index=distance_matrix.ids)
    predictions.index.name = 'SampleID'
    pred = qiime2.Artifact.import_data(
        'SampleData[ClassifierPredictions]', predictions)

    confusion = ctx.get_action('sample_classifier', 'confusion_matrix')
    accuracy_results, = confusion(
        pred, metadata, missing_samples='ignore', palette=palette)

    return pred, accuracy_results


def classify_samples(ctx,
                     table,
                     metadata,
                     test_size=defaults['test_size'],
                     step=defaults['step'],
                     cv=defaults['cv'],
                     random_state=None,
                     n_jobs=defaults['n_jobs'],
                     n_estimators=defaults['n_estimators'],
                     estimator=defaults['estimator_c'],
                     optimize_feature_selection=False,
                     parameter_tuning=False,
                     palette=defaults['palette'],
                     missing_samples=defaults['missing_samples']):

    split = ctx.get_action('sample_classifier', 'split_table')
    fit = ctx.get_action('sample_classifier', 'fit_classifier')
    predict_test = ctx.get_action(
        'sample_classifier', 'predict_classification')
    summarize_estimator = ctx.get_action('sample_classifier', 'summarize')
    confusion = ctx.get_action('sample_classifier', 'confusion_matrix')

    X_train, X_test = split(table, metadata, test_size, random_state,
                            stratify=True, missing_samples=missing_samples)

    sample_estimator, importance = fit(
        X_train, metadata, step, cv, random_state, n_jobs, n_estimators,
        estimator, optimize_feature_selection, parameter_tuning,
        missing_samples='ignore')

    predictions, = predict_test(X_test, sample_estimator, n_jobs)

    summary, = summarize_estimator(sample_estimator)

    accuracy_results, = confusion(
        predictions, metadata, missing_samples='ignore', palette=palette)

    return sample_estimator, importance, predictions, summary, accuracy_results


def regress_samples(ctx,
                    table,
                    metadata,
                    test_size=defaults['test_size'],
                    step=defaults['step'],
                    cv=defaults['cv'],
                    random_state=None,
                    n_jobs=defaults['n_jobs'],
                    n_estimators=defaults['n_estimators'],
                    estimator=defaults['estimator_r'],
                    optimize_feature_selection=False,
                    stratify=False,
                    parameter_tuning=False,
                    missing_samples=defaults['missing_samples']):

    split = ctx.get_action('sample_classifier', 'split_table')
    fit = ctx.get_action('sample_classifier', 'fit_regressor')
    predict_test = ctx.get_action('sample_classifier', 'predict_regression')
    summarize_estimator = ctx.get_action('sample_classifier', 'summarize')
    scatter = ctx.get_action('sample_classifier', 'scatterplot')

    X_train, X_test = split(table, metadata, test_size, random_state,
                            stratify, missing_samples=missing_samples)

    sample_estimator, importance = fit(
        X_train, metadata, step, cv, random_state, n_jobs, n_estimators,
        estimator, optimize_feature_selection, parameter_tuning,
        missing_samples='ignore')

    predictions, = predict_test(X_test, sample_estimator, n_jobs)

    summary, = summarize_estimator(sample_estimator)

    accuracy_results, = scatter(predictions, metadata, 'ignore')

    return sample_estimator, importance, predictions, summary, accuracy_results


# this action has been replaced by the classify_samples pipeline and is no
# longer registered. Will be removed in a separate PR.
def classify_samples_basic(output_dir: str, table: biom.Table,
                           metadata: qiime2.CategoricalMetadataColumn,
                           test_size: float=defaults['test_size'],
                           step: float=defaults['step'],
                           cv: int=defaults['cv'], random_state: int=None,
                           n_jobs: int=defaults['n_jobs'],
                           n_estimators: int=defaults['n_estimators'],
                           estimator: str=defaults['estimator_c'],
                           optimize_feature_selection: bool=False,
                           parameter_tuning: bool=False,
                           palette: str=defaults['palette'],
                           missing_samples: str=defaults['missing_samples']
                           ) -> None:

    # extract column name from CategoricalMetadataColumn
    column = metadata.name

    # disable feature selection for unsupported estimators
    optimize_feature_selection, calc_feature_importance = \
        _disable_feature_selection(estimator, optimize_feature_selection)

    # specify parameters and distributions to sample from for parameter tuning
    estimator, param_dist, parameter_tuning = _set_parameters_and_estimator(
        estimator, table, metadata, column, n_estimators, n_jobs, cv,
        random_state, parameter_tuning, classification=True,
        missing_samples=missing_samples)

    estimator, cm, accuracy, importances = split_optimize_classify(
        table, metadata, column, estimator, output_dir,
        test_size=test_size, step=step, cv=cv, random_state=random_state,
        n_jobs=n_jobs, optimize_feature_selection=optimize_feature_selection,
        parameter_tuning=parameter_tuning, param_dist=param_dist,
        calc_feature_importance=calc_feature_importance, palette=palette,
        missing_samples=missing_samples)

    _visualize(output_dir, estimator, cm, importances,
               optimize_feature_selection, title='classification predictions')


def fit_classifier(table: biom.Table,
                   metadata: qiime2.CategoricalMetadataColumn,
                   step: float=defaults['step'], cv: int=defaults['cv'],
                   random_state: int=None, n_jobs: int=defaults['n_jobs'],
                   n_estimators: int=defaults['n_estimators'],
                   estimator: str=defaults['estimator_c'],
                   optimize_feature_selection: bool=False,
                   parameter_tuning: bool=False,
                   missing_samples: str=defaults['missing_samples']
                   ) -> (Pipeline, pd.DataFrame):
    estimator, importance = _fit_estimator(
        table, metadata, estimator, n_estimators, step, cv, random_state,
        n_jobs, optimize_feature_selection, parameter_tuning,
        missing_samples=missing_samples, classification=True)

    return estimator, importance


def fit_regressor(table: biom.Table,
                  metadata: qiime2.CategoricalMetadataColumn,
                  step: float=defaults['step'], cv: int=defaults['cv'],
                  random_state: int=None, n_jobs: int=defaults['n_jobs'],
                  n_estimators: int=defaults['n_estimators'],
                  estimator: str=defaults['estimator_r'],
                  optimize_feature_selection: bool=False,
                  parameter_tuning: bool=False,
                  missing_samples: str=defaults['missing_samples']
                  ) -> (Pipeline, pd.DataFrame):
    estimator, importance = _fit_estimator(
        table, metadata, estimator, n_estimators, step, cv, random_state,
        n_jobs, optimize_feature_selection, parameter_tuning,
        missing_samples=missing_samples, classification=False)

    return estimator, importance


# this action has been replaced by the regress_samples pipeline and is no
# longer registered. Will be removed in a separate PR.
def regress_samples_basic(output_dir: str, table: biom.Table,
                          metadata: qiime2.NumericMetadataColumn,
                          test_size: float=defaults['test_size'],
                          step: float=defaults['step'],
                          cv: int=defaults['cv'], random_state: int=None,
                          n_jobs: int=defaults['n_jobs'],
                          n_estimators: int=defaults['n_estimators'],
                          estimator: str=defaults['estimator_r'],
                          optimize_feature_selection: bool=False,
                          stratify: str=False, parameter_tuning: bool=False,
                          missing_samples: str=defaults['missing_samples']
                          ) -> None:

    # extract column name from NumericMetadataColumn
    column = metadata.name

    # disable feature selection for unsupported estimators
    optimize_feature_selection, calc_feature_importance = \
        _disable_feature_selection(estimator, optimize_feature_selection)

    # specify parameters and distributions to sample from for parameter tuning
    estimator, param_dist, parameter_tuning = _set_parameters_and_estimator(
        estimator, table, metadata, column, n_estimators, n_jobs, cv,
        random_state, parameter_tuning, classification=True,
        missing_samples=missing_samples)

    estimator, cm, accuracy, importances = split_optimize_classify(
        table, metadata, column, estimator, output_dir,
        test_size=test_size, step=step, cv=cv, random_state=random_state,
        n_jobs=n_jobs, optimize_feature_selection=optimize_feature_selection,
        parameter_tuning=parameter_tuning, param_dist=param_dist,
        calc_feature_importance=calc_feature_importance,
        scoring=mean_squared_error, stratify=stratify, classification=False,
        missing_samples=missing_samples)

    _visualize(output_dir, estimator, cm, importances,
               optimize_feature_selection, title='regression predictions')


def predict_base(table, sample_estimator, n_jobs):
    # extract feature data from biom
    feature_data = _extract_features(table)

    # reset n_jobs if this is a valid parameter for the estimator
    if 'est__n_jobs' in sample_estimator.get_params().keys():
        sample_estimator.set_params(est__n_jobs=n_jobs)

    # predict values and output as series
    y_pred = sample_estimator.predict(feature_data)
    # need to flatten arrays that come out as multidimensional
    y_pred = y_pred.flatten()
    y_pred = pd.Series(y_pred, index=table.ids(), name='prediction')
    y_pred.index.name = 'SampleID'

    return y_pred


def predict_classification(table: biom.Table, sample_estimator: Pipeline,
                           n_jobs: int=defaults['n_jobs']) -> pd.Series:
    return predict_base(table, sample_estimator, n_jobs)


def predict_regression(table: biom.Table, sample_estimator: Pipeline,
                       n_jobs: int=defaults['n_jobs']) -> pd.Series:
    return predict_base(table, sample_estimator, n_jobs)


def split_table(table: biom.Table, metadata: qiime2.MetadataColumn,
                test_size: float=defaults['test_size'], random_state: int=None,
                stratify: str=True,
                missing_samples: str=defaults['missing_samples']
                ) -> (biom.Table, biom.Table):
    column = metadata.name
    X_train, X_test, y_train, y_test = _prepare_training_data(
        table, metadata, column, test_size, random_state, load_data=True,
        stratify=True, missing_samples=missing_samples)
    # TODO: we can consider returning the metadata (y_train, y_test) if a
    # SampleData[Metadata] type comes into existence. For now we will just
    # throw this out.
    return X_train, X_test


def regress_samples_ncv(
        table: biom.Table, metadata: qiime2.NumericMetadataColumn,
        cv: int=defaults['cv'], random_state: int=None,
        n_jobs: int=defaults['n_jobs'],
        n_estimators: int=defaults['n_estimators'],
        estimator: str=defaults['estimator_r'], stratify: str=False,
        parameter_tuning: bool=False,
        missing_samples: str=defaults['missing_samples']
        ) -> (pd.Series, pd.DataFrame):

    y_pred, importances = nested_cross_validation(
        table, metadata, cv, random_state, n_jobs, n_estimators, estimator,
        stratify, parameter_tuning, classification=False,
        scoring=mean_squared_error, missing_samples=missing_samples)
    return y_pred, importances


def classify_samples_ncv(
        table: biom.Table, metadata: qiime2.CategoricalMetadataColumn,
        cv: int=defaults['cv'], random_state: int=None,
        n_jobs: int=defaults['n_jobs'],
        n_estimators: int=defaults['n_estimators'],
        estimator: str=defaults['estimator_c'],
        parameter_tuning: bool=False,
        missing_samples: str=defaults['missing_samples']
        ) -> (pd.Series, pd.DataFrame):

    y_pred, importances = nested_cross_validation(
        table, metadata, cv, random_state, n_jobs, n_estimators, estimator,
        stratify=True, parameter_tuning=parameter_tuning, classification=False,
        scoring=accuracy_score, missing_samples=missing_samples)
    return y_pred, importances


def scatterplot(output_dir: str, predictions: pd.Series,
                truth: qiime2.NumericMetadataColumn,
                missing_samples: str=defaults['missing_samples']) -> None:
    predictions = pd.to_numeric(predictions)

    _plot_accuracy(output_dir, predictions, truth, missing_samples,
                   classification=False, palette=None,
                   plot_title='regression scatterplot')


def confusion_matrix(output_dir: str, predictions: pd.Series,
                     truth: qiime2.CategoricalMetadataColumn,
                     missing_samples: str=defaults['missing_samples'],
                     palette: str=defaults['palette']) -> None:
    _plot_accuracy(output_dir, predictions, truth, missing_samples,
                   classification=True, palette=palette,
                   plot_title='confusion matrix')


def summarize(output_dir: str, sample_estimator: Pipeline):
    _summarize_estimator(output_dir, sample_estimator)


# The following method is experimental and is not registered in the current
# release. Any use of the API is at user's own risk.
def detect_outliers(table: biom.Table,
                    metadata: qiime2.Metadata, subset_column: str=None,
                    subset_value: str=None,
                    n_estimators: int=defaults['n_estimators'],
                    contamination: float=0.05, random_state: int=None,
                    n_jobs: int=defaults['n_jobs'],
                    missing_samples: str='ignore') -> (pd.Series):

    features, sample_md = _load_data(
        table, metadata, missing_samples=missing_samples)

    # if opting to train on a subset, choose subset that fits criteria
    if subset_column and subset_value:
        X_train = \
            [f for s, f in
             zip(sample_md[subset_column] == subset_value, features) if s]
    # raise error if subset_column or subset_value (but not both) are set
    elif subset_column is not None or subset_value is not None:
        raise ValueError((
            'subset_column and subset_value must both be provided with a '
            'valid value to perform model training on a subset of data.'))
    else:
        X_train = features

    # fit isolation tree
    estimator = Pipeline(
        [('dv', DictVectorizer()),
         ('est', IsolationForest(n_jobs=n_jobs, n_estimators=n_estimators,
                                 contamination=contamination,
                                 random_state=random_state))])
    estimator.fit(X_train)

    # predict outlier status
    y_pred = estimator.predict(features)
    y_pred = pd.Series(y_pred, index=sample_md.index)
    # predict reports whether sample is an inlier; change to outlier status
    y_pred[y_pred == -1] = 'True'
    y_pred[y_pred == 1] = 'False'
    y_pred.name = "outlier"
    return y_pred
