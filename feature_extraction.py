import category_encoders as ce
import numpy as np
import pandas as pd
from sklearn.model_selection import KFold
from sklearn.externals import joblib
from sklearn import preprocessing as prep

from steps.base import BaseTransformer
from steps.utils import get_logger

logger = get_logger()


class DataFrameByTypeSplitter(BaseTransformer):
    def __init__(self, numerical_columns, categorical_columns, timestamp_columns):
        self.numerical_columns = numerical_columns
        self.categorical_columns = categorical_columns
        self.timestamp_columns = timestamp_columns

    def transform(self, X, y=None, **kwargs):
        outputs = {}

        if self.numerical_columns is not None:
            outputs['numerical_features'] = X[self.numerical_columns]

        if self.categorical_columns is not None:
            outputs['categorical_features'] = X[self.categorical_columns]

        if self.timestamp_columns is not None:
            outputs['timestamp_features'] = X[self.timestamp_columns]

        return outputs


class FeatureJoiner(BaseTransformer):
    def transform(self, numerical_feature_list, categorical_feature_list, **kwargs):
        features = numerical_feature_list + categorical_feature_list
        for feature in features:
            feature.reset_index(drop=True, inplace=True)

        outputs = {}
        outputs['features'] = pd.concat(features, axis=1).astype(np.float32)
        outputs['feature_names'] = self._get_feature_names(features)
        outputs['categorical_features'] = self._get_feature_names(categorical_feature_list)
        return outputs

    def _get_feature_names(self, dataframes):
        feature_names = []
        for dataframe in dataframes:
            feature_names.extend(list(dataframe.columns))
        return feature_names


class CategoricalFilter(BaseTransformer):
    def __init__(self, categorical_columns, min_frequencies, impute_value=np.nan):
        self.categorical_columns = categorical_columns
        self.min_frequencies = min_frequencies
        self.impute_value = impute_value
        self.category_levels_to_remove = {}

    def fit(self, categorical_features):
        for column, threshold in zip(self.categorical_columns, self.min_frequencies):
            value_counts = categorical_features[column].value_counts()
            self.category_levels_to_remove[column] = value_counts[value_counts <= threshold].index.tolist()
        return self

    def transform(self, categorical_features):
        for column, levels_to_remove in self.category_levels_to_remove.items():
            if levels_to_remove:
                categorical_features[column].replace(levels_to_remove, self.impute_value, inplace=True)
            categorical_features['{}_infrequent'.format(column)] = categorical_features[column] == self.impute_value
            categorical_features['{}_infrequent'.format(column)] = categorical_features[
                '{}_infrequent'.format(column)].astype(int)
        return {'categorical_features': categorical_features}

    def load(self, filepath):
        params = joblib.load(filepath)
        self.categorical_columns = params['categorical_columns']
        self.min_frequencies = params['min_frequencies']
        self.impute_value = params['impute_value']
        self.category_levels_to_remove = params['category_levels_to_remove']
        return self

    def save(self, filepath):
        params = {}
        params['categorical_columns'] = self.categorical_columns
        params['min_frequencies'] = self.min_frequencies
        params['impute_value'] = self.impute_value
        params['category_levels_to_remove'] = self.category_levels_to_remove
        joblib.dump(params, filepath)


class TargetEncoder(BaseTransformer):
    def __init__(self, **kwargs):
        self.params = kwargs
        self.encoder_class = ce.TargetEncoder

    def fit(self, X, y, **kwargs):
        categorical_columns = list(X.columns)
        self.target_encoder = self.encoder_class(cols=categorical_columns, **self.params)
        self.target_encoder.fit(X, y)
        return self

    def transform(self, X, y=None, **kwargs):
        X_ = self.target_encoder.transform(X)
        return {'numerical_features': X_}

    def load(self, filepath):
        self.target_encoder = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.target_encoder, filepath)


class TargetEncoderNSplits(BaseTransformer):
    def __init__(self, n_splits, **kwargs):
        self.k_folds = KFold(n_splits=n_splits)
        self.target_means_map = {}

    def _target_means_names(self, columns):
        confidence_rate_names = ['target_mean_{}'.format(column) for column in columns]
        return confidence_rate_names

    def _is_null_names(self, columns):
        is_null_names = ['target_mean_is_missing_{}'.format(column) for column in columns]
        return is_null_names

    def fit(self, categorical_features, target, **kwargs):
        feature_columns, target_column = categorical_features.columns, target.columns[0]

        X_target_means = []
        self.k_folds.get_n_splits(target)
        for train_index, test_index in self.k_folds.split(target):
            X_train, y_train = categorical_features.iloc[train_index], target.iloc[train_index]
            X_test, y_test = categorical_features.iloc[test_index], target.iloc[test_index]

            train = pd.concat([X_train, y_train], axis=1)
            for column, target_mean_name in zip(feature_columns, self._target_means_names(feature_columns)):
                group_object = train.groupby(column)
                train_target_means = group_object[target_column].mean(). \
                    reset_index().rename(index=str, columns={target_column: target_mean_name})

                X_test = X_test.merge(train_target_means, on=column, how='left')
            X_target_means.append(X_test)
        X_target_means = pd.concat(X_target_means, axis=0)

        for column, target_mean_name in zip(feature_columns, self._target_means_names(feature_columns)):
            group_object = X_target_means.groupby(column)
            self.target_means_map[column] = group_object[target_mean_name].mean().reset_index()

        return self

    def transform(self, categorical_features, **kwargs):
        columns = categorical_features.columns

        for column, target_mean_name, is_null_name in zip(columns,
                                                          self._target_means_names(columns),
                                                          self._is_null_names(columns)):
            categorical_features = categorical_features.merge(self.target_means_map[column],
                                                              on=column,
                                                              how='left')
            categorical_features[is_null_name] = pd.isnull(categorical_features[target_mean_name]).astype(int)
            categorical_features[target_mean_name].fillna(0, inplace=True)

        return {'numerical_features': categorical_features[self._target_means_names(columns)].astype(np.float32),
                'categorical_features': categorical_features[self._is_null_names(columns)]}

    def load(self, filepath):
        self.target_means_map = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.target_means_map, filepath)


class BinaryEncoder(BaseTransformer):
    def __init__(self, **kwargs):
        self.params = kwargs
        self.encoder_class = ce.binary.BinaryEncoder

    def fit(self, X, **kwargs):
        categorical_columns = list(X.columns)
        self.binary_encoder = self.encoder_class(cols=categorical_columns, **self.params)
        self.binary_encoder.fit(X)
        return self

    def transform(self, X, **kwargs):
        X_ = self.binary_encoder.transform(X)
        return {'numerical_features': X_}

    def load(self, filepath):
        self.target_encoder = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.target_encoder, filepath)


class TimeDelta(BaseTransformer):
    def __init__(self, groupby_specs, timestamp_column):
        self.groupby_specs = groupby_specs
        self.timestamp_column = timestamp_column

    @property
    def time_delta_names(self):
        time_delta_names = ['time_delta_{}'.format('_'.join(groupby_spec))
                            for groupby_spec in self.groupby_specs]
        return time_delta_names

    @property
    def is_null_names(self):
        is_null_names = ['time_delta_is_missing_{}'.format('_'.join(groupby_spec))
                         for groupby_spec in self.groupby_specs]
        return is_null_names

    def transform(self, categorical_features, timestamp_features):
        X = pd.concat([categorical_features, timestamp_features], axis=1)
        for groupby_spec, time_delta_name, is_null_name in zip(self.groupby_specs,
                                                               self.time_delta_names,
                                                               self.is_null_names):
            X[time_delta_name] = X.groupby(groupby_spec)[self.timestamp_column].apply(self._time_delta).reset_index(
                level=list(range(len(groupby_spec))), drop=True)
            X[is_null_name] = pd.isnull(X[time_delta_name]).astype(int)
            X[time_delta_name].fillna(0, inplace=True)
        return {'numerical_features': X[self.time_delta_names].astype(np.float32),
                'categorical_features': X[self.is_null_names]}

    def _time_delta(self, groupby_object):
        if len(groupby_object) == 1:
            return pd.Series(np.nan, index=groupby_object.index)
        else:
            groupby_object = groupby_object.sort_values().diff().dt.seconds
            return groupby_object


class GroupbyAggregations(BaseTransformer):
    def __init__(self, groupby_aggregations):
        self.groupby_aggregations = groupby_aggregations

    @property
    def groupby_aggregations_names(self):
        groupby_aggregations_names = ['{}_{}_{}'.format('_'.join(spec['groupby']), spec['agg'], spec['select'])
                                      for spec in self.groupby_aggregations]
        return groupby_aggregations_names

    def transform(self, categorical_features):
        for spec, groupby_aggregations_name in zip(self.groupby_aggregations, self.groupby_aggregations_names):
            logger.info('processing {}'.format(groupby_aggregations_name))
            group_object = categorical_features.groupby(spec['groupby'])

            categorical_features = categorical_features.merge(
                group_object[spec['select']].agg(spec['agg']).reset_index().rename(index=str, columns={
                    spec['select']: groupby_aggregations_name})[spec['groupby'] + [groupby_aggregations_name]],
                on=spec['groupby'], how='left')

        return {'numerical_features': categorical_features[self.groupby_aggregations_names].astype(np.float32)}


class IsMissing(BaseTransformer):
    def __init__(self, columns):
        self.columns = columns

    @property
    def missing_names(self):
        return ['{}_is_missing'.format(col) for col in self.columns]

    def transform(self, X, **kwargs):
        for name, missing_name in zip(self.columns, self.missing_names):
            X[missing_name] = pd.isnull(X[name]).astype(int)
        return {'categorical_features': X[self.missing_names]}


class HashingCategoricalEncoder(BaseTransformer):
    def __init__(self, **kwargs):
        self.hashing_encoder = ce.HashingEncoder(**kwargs)

    def fit(self, categorical_features, **kwargs):
        self.hashing_encoder.fit(categorical_features)
        return self

    def transform(self, categorical_features, **kwargs):
        categorical_features = self.hashing_encoder.transform(categorical_features)
        return {'categorical_features': categorical_features}

    def load(self, filepath):
        self.hashing_encoder = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.hashing_encoder, filepath)


class CategoricalEncoder(BaseTransformer):
    def __init__(self, columns_to_encode):
        self.columns_to_encode = columns_to_encode
        self.columns_with_encoders = [(col_name, prep.LabelEncoder()) for col_name in columns_to_encode]

    def fit(self, categorical_features, **kwargs):
        for column_name, encoder in self.columns_with_encoders:
            logger.info('fitting {}'.format(column_name))
            encoder.fit(categorical_features[column_name].astype(str).values)
        return self

    def transform(self, categorical_features, **kwargs):
        for column_name, encoder in self.columns_with_encoders:
            logger.info('transforming {}'.format(column_name))
            categorical_features[column_name], encoder = self._input_unknown(categorical_features[column_name], encoder)
            categorical_features[column_name] = encoder.transform(categorical_features[column_name].astype(str).values)
        return {'categorical_features': categorical_features}

    def _input_unknown(self, column, encoder):
        def func(x):
            return '<unknown>' if x not in encoder.classes_ else x

        column = column.apply(func)
        encoder.classes_ = np.append(encoder.classes_, '<unknown>')
        return column, encoder

    def load(self, filepath):
        self.columns_with_encoders = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.columns_with_encoders, filepath)


class DateFeatures(BaseTransformer):
    def __init__(self, date_column):
        self.date_column = date_column

    @property
    def date_features_names(self):
        date_features_names = ['{}_month'.format(self.date_column),
                               '{}_day'.format(self.date_column),
                               '{}_weekday'.format(self.date_column),
                               ]
        return date_features_names

    def transform(self, timestamp_features, **kwargs):
        date_index = pd.DatetimeIndex(timestamp_features[self.date_column])
        timestamp_features['{}_month'.format(self.date_column)] = date_index.month
        timestamp_features['{}_day'.format(self.date_column)] = date_index.day
        timestamp_features['{}_weekday'.format(self.date_column)] = date_index.weekday
        return {'categorical_features': timestamp_features[self.date_features_names].astype(int)}


class Blacklist(BaseTransformer):
    def __init__(self, blacklist):
        self.blacklist = blacklist

    @property
    def blacklist_names(self):
        blacklist_names = ['{}_on_blacklist'.format(category) for category in self.blacklist]
        return blacklist_names

    def transform(self, categorical_features):
        for category, blacklist_name in zip(self.blacklist, self.blacklist_names):
            categorical_features[blacklist_name] = (
                categorical_features[category].isin(self.blacklist[category])).astype(int)

        return {'categorical_features': categorical_features[self.blacklist_names]}


class ConfidenceRate(BaseTransformer):
    def __init__(self, confidence_level=100, categories=[]):
        self.confidence_level = confidence_level
        self.categories = categories
        self.confidence_rates_map = {}

    @property
    def confidence_rate_names(self):
        confidence_rate_names = ['confidence_rate_{}'.format('_'.join(category))
                                 for category in self.categories]
        return confidence_rate_names

    @property
    def is_null_names(self):
        is_null_names = ['confidence_rate_is_nan_{}'.format('_'.join(category))
                         for category in self.categories]
        return is_null_names

    def fit(self, categorical_features, target):
        concatenated_dataframe = pd.concat([categorical_features, target], axis=1)

        for category, confidence_rate_name in zip(self.categories, self.confidence_rate_names):
            group_object = concatenated_dataframe.groupby(category)

            self.confidence_rates_map['_'.join(category)] = \
                group_object['is_attributed'].apply(self._rate_calculation).reset_index().rename(
                    index=str,
                    columns={'is_attributed': confidence_rate_name})[category + [confidence_rate_name]]

        return self

    def transform(self, categorical_features, **kwargs):

        for category, confidence_rate_name, is_null_name in zip(self.categories,
                                                                self.confidence_rate_names,
                                                                self.is_null_names):
            categorical_features = categorical_features.merge(self.confidence_rates_map['_'.join(category)],
                                                              on=category,
                                                              how='left')
            categorical_features[is_null_name] = pd.isnull(categorical_features[confidence_rate_name]).astype(int)
            categorical_features[confidence_rate_name].fillna(0, inplace=True)

        return {'numerical_features': categorical_features[self.confidence_rate_names].astype(np.float32),
                'categorical_features': categorical_features[self.is_null_names]}

    def load(self, filepath):
        self.confidence_rates_map = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.confidence_rates_map, filepath)

    def _rate_calculation(self, x):
        rate = x.sum() / float(x.count())
        confidence = np.min([1, np.log(x.count()) / np.log(self.confidence_level)])

        return rate * confidence * 100
