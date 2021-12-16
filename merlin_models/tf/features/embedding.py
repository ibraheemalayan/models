#
# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Union

import tensorflow as tf
from tensorflow.python import to_dlpack
from tensorflow.python.keras import backend
from tensorflow.python.tpu.tpu_embedding_v2_utils import FeatureConfig, TableConfig

from merlin_models.tf.block.transformations import AsSparseFeatures
from merlin_standard_lib import Schema
from merlin_standard_lib.schema.tag import Tag, TagsType
from merlin_standard_lib.utils.doc_utils import docstring_parameter
from merlin_standard_lib.utils.embedding_utils import get_embedding_sizes_from_schema

from ..core import (
    TABULAR_MODULE_PARAMS_DOCSTRING,
    Block,
    BlockType,
    Filter,
    SequentialBlock,
    TabularAggregationType,
    TabularInputBlock,
)

# pylint has issues with TF array ops, so disable checks until fixed:
# https://github.com/PyCQA/pylint/issues/3613
# pylint: disable=no-value-for-parameter, unexpected-keyword-arg
from ..typing import TabularData

EMBEDDING_FEATURES_PARAMS_DOCSTRING = """
    feature_config: Dict[str, FeatureConfig]
        This specifies what TableConfig to use for each feature. For shared embeddings, the same
        TableConfig can be used for multiple features.
    item_id: str, optional
        The name of the feature that's used for the item_id.
"""


@dataclass
class EmbeddingOptions:
    embedding_dim_default: Optional[int] = 64
    infer_embedding_sizes: bool = False
    infer_embedding_sizes_multiplier: float = 2.0
    embeddings_initializers: Optional[Dict[str, Callable[[Any], None]]] = None
    combiner: Optional[str] = "mean"


@docstring_parameter(
    tabular_module_parameters=TABULAR_MODULE_PARAMS_DOCSTRING,
    embedding_features_parameters=EMBEDDING_FEATURES_PARAMS_DOCSTRING,
)
@tf.keras.utils.register_keras_serializable(package="merlin_models")
class EmbeddingFeatures(TabularInputBlock):
    """Input block for embedding-lookups for categorical features.

    For multi-hot features, the embeddings will be aggregated into a single tensor using the mean.

    Parameters
    ----------
    {embedding_features_parameters}
    {tabular_module_parameters}
    """

    def __init__(
        self,
        feature_config: Dict[str, "FeatureConfig"],
        pre: Optional[BlockType] = None,
        post: Optional[BlockType] = None,
        aggregation: Optional[TabularAggregationType] = None,
        schema: Optional[Schema] = None,
        name=None,
        add_default_pre=True,
        **kwargs,
    ):
        if add_default_pre:
            embedding_pre = [Filter(list(feature_config.keys())), AsSparseFeatures()]
            pre = [embedding_pre, pre] if pre else embedding_pre  # type: ignore
        self.feature_config = feature_config

        super().__init__(
            pre=pre, post=post, aggregation=aggregation, name=name, schema=schema, **kwargs
        )

    @classmethod
    def from_schema(  # type: ignore
        cls,
        schema: Schema,
        embedding_dims: Optional[Dict[str, int]] = None,
        options: EmbeddingOptions = EmbeddingOptions(),
        tags: Optional[TagsType] = None,
        max_sequence_length: Optional[int] = None,
        **kwargs,
    ) -> Optional["EmbeddingFeatures"]:
        schema_copy = schema.copy()

        if tags:
            schema_copy = schema_copy.select_by_tag(tags)

        if options.infer_embedding_sizes:
            embedding_dims = get_embedding_sizes_from_schema(
                schema, options.infer_embedding_sizes_multiplier
            )

        embedding_dims = embedding_dims or {}
        embeddings_initializers = options.embeddings_initializers or {}

        emb_config = {}
        cardinalities = schema.categorical_cardinalities()
        domains = schema.categorical_domains()
        for key, cardinality in cardinalities.items():
            embedding_size = embedding_dims.get(key, options.embedding_dim_default)
            embedding_initializer = embeddings_initializers.get(key, None)
            emb_config[key] = (cardinality, embedding_size, embedding_initializer)

        feature_config: Dict[str, FeatureConfig] = {}
        tables: Dict[str, TableConfig] = {}
        for name, (vocab_size, dim, emb_initilizer) in emb_config.items():
            table_name = domains[name]
            table = tables.get(table_name, None)
            if not table:
                table = TableConfig(
                    vocabulary_size=vocab_size,
                    dim=dim,
                    name=table_name,
                    combiner=options.combiner,
                    initializer=emb_initilizer,
                )
                tables[table_name] = table
            feature_config[name] = FeatureConfig(table)

        if not feature_config:
            return None

        output = cls(feature_config, schema=schema_copy, **kwargs)

        return output

    def build(self, input_shapes):
        self.embedding_tables = {}
        tables: Dict[str, TableConfig] = {}
        for name, feature in self.feature_config.items():
            table: TableConfig = feature.table
            if table.name not in tables:
                tables[table.name] = table

        for name, table in tables.items():
            add_fn = (
                self.context.add_embedding_weight if hasattr(self, "_context") else self.add_weight
            )
            self.embedding_tables[name] = add_fn(
                name=name,
                trainable=True,
                initializer=table.initializer,
                shape=(table.vocabulary_size, table.dim),
            )
        if isinstance(input_shapes, dict):
            super().build(input_shapes)
        else:
            tf.keras.layers.Layer.build(self, input_shapes)

    def call(self, inputs: TabularData, **kwargs) -> TabularData:
        embedded_outputs = {}
        for name, val in inputs.items():
            embedded_outputs[name] = self.lookup_feature(name, val)

        return embedded_outputs

    def compute_call_output_shape(self, input_shapes):
        batch_size = self.calculate_batch_size_from_input_shapes(input_shapes)

        output_shapes = {}
        for name, val in input_shapes.items():
            output_shapes[name] = tf.TensorShape([batch_size, self.feature_config[name].table.dim])

        return output_shapes

    def lookup_feature(self, name, val, output_sequence=False):
        dtype = backend.dtype(val)
        if dtype != "int32" and dtype != "int64":
            val = tf.cast(val, "int32")

        table: TableConfig = self.feature_config[name].table
        table_var = self.embedding_tables[table.name]
        if isinstance(val, tf.SparseTensor):
            out = tf.nn.safe_embedding_lookup_sparse(table_var, val, None, combiner=table.combiner)
        else:
            if output_sequence:
                out = tf.gather(table_var, tf.cast(val, tf.int32))
            else:
                if len(val.shape) > 1:
                    # TODO: Check if it is correct to retrieve only the 1st element
                    # of second dim for non-sequential multi-hot categ features
                    out = tf.gather(table_var, tf.cast(val, tf.int32)[:, 0])
                else:
                    out = tf.gather(table_var, tf.cast(val, tf.int32))

        if self._dtype_policy.compute_dtype != self._dtype_policy.variable_dtype:
            # Instead of casting the variable as in most layers, cast the output, as
            # this is mathematically equivalent but is faster.
            out = tf.cast(out, self._dtype_policy.compute_dtype)

        return out

    def table_config(self, feature_name: str):
        return self.feature_config[feature_name].table

    def embedding_table_df(self, table_name: Union[str, Tag], gpu=True):
        embeddings = self.embedding_tables[str(table_name)]

        if gpu:
            import cudf

            df = cudf.from_dlpack(to_dlpack(tf.convert_to_tensor(embeddings)))
            df.columns = [str(col) for col in list(df.columns)]
            df.set_index(cudf.RangeIndex(0, embeddings.shape[0]))
        else:
            import pandas as pd

            df = pd.DataFrame(embeddings.numpy())
            df.columns = [str(col) for col in list(df.columns)]
            df.set_index(pd.RangeIndex(0, embeddings.shape[0]))

        return df

    def export_embedding_table(self, table_name: Union[str, Tag], export_path: str, gpu=True):
        df = self.embedding_table_df(table_name, gpu=gpu)
        df.to_parquet(export_path)

    def get_config(self):
        config = super().get_config()

        feature_configs = {}

        for key, val in self.feature_config.items():
            feature_config_dict = dict(name=val.name, max_sequence_length=val.max_sequence_length)

            feature_config_dict["table"] = serialize_table_config(val.table)
            feature_configs[key] = feature_config_dict

        config["feature_config"] = feature_configs

        return config

    @classmethod
    def from_config(cls, config):
        # Deserialize feature_config
        feature_configs, table_configs = {}, {}
        for key, val in config["feature_config"].items():
            feature_params = deepcopy(val)
            table_params = feature_params["table"]
            if "name" in table_configs:
                feature_params["table"] = table_configs["name"]
            else:
                table = deserialize_table_config(table_params)
                if table.name:
                    table_configs[table.name] = table
                feature_params["table"] = table
            feature_configs[key] = FeatureConfig(**feature_params)
        config["feature_config"] = feature_configs

        # Set `add_default_pre to False` since pre will be provided from the config
        config["add_default_pre"] = False

        return super().from_config(config)


@docstring_parameter(
    tabular_module_parameters=TABULAR_MODULE_PARAMS_DOCSTRING,
    embedding_features_parameters=EMBEDDING_FEATURES_PARAMS_DOCSTRING,
)
@tf.keras.utils.register_keras_serializable(package="merlin_models")
class SequenceEmbeddingFeatures(EmbeddingFeatures):
    """Input block for embedding-lookups for categorical features. This module produces 3-D tensors,
    this is useful for sequential models like transformers.
    Parameters
    ----------
    {embedding_features_parameters}
    padding_idx: int
        The symbol to use for padding.
    {tabular_module_parameters}
    """

    def __init__(
        self,
        feature_config: Dict[str, FeatureConfig],
        mask_zero: bool = True,
        padding_idx: int = 0,
        pre: Optional[BlockType] = None,
        post: Optional[BlockType] = None,
        aggregation: Optional[TabularAggregationType] = None,
        schema: Optional[Schema] = None,
        name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(
            feature_config,
            pre=pre,
            post=post,
            aggregation=aggregation,
            schema=schema,
            name=name,
            **kwargs,
        )
        self.padding_idx = padding_idx
        self.mask_zero = mask_zero

    def lookup_feature(self, name, val, **kwargs):
        return super(SequenceEmbeddingFeatures, self).lookup_feature(
            name, val, output_sequence=True
        )

    def compute_call_output_shape(self, input_shapes):
        batch_size = self.calculate_batch_size_from_input_shapes(input_shapes)
        sequence_length = input_shapes[list(self.feature_config.keys())[0]][1]

        output_shapes = {}
        for name, val in input_shapes.items():
            output_shapes[name] = tf.TensorShape(
                [batch_size, sequence_length, self.feature_config[name].table.dim]
            )

        return output_shapes

    def compute_mask(self, inputs, mask=None):
        if not self.mask_zero:
            return None
        outputs = {}
        for key, val in inputs.items():
            outputs[key] = tf.not_equal(val, self.padding_idx)

        return outputs

    def get_config(self):
        config = super().get_config()
        config["mask_zero"] = self.mask_zero
        config["padding_idx"] = self.padding_idx

        return config


def ContinuousEmbedding(
    inputs: Block,
    embedding_block: Block,
    aggregation=None,
    continuous_aggregation="concat",
    name: str = "continuous",
    **kwargs,
) -> SequentialBlock:
    continuous_embedding = Filter(Tag.CONTINUOUS, aggregation=continuous_aggregation).connect(
        embedding_block
    )

    outputs = inputs.connect_branch(
        continuous_embedding.as_tabular(name), add_rest=True, aggregation=aggregation, **kwargs
    )

    return outputs


def serialize_table_config(table_config: TableConfig) -> Dict[str, Any]:
    table = deepcopy(table_config.__dict__)
    if "initializer" in table:
        table["initializer"] = tf.keras.initializers.serialize(table["initializer"])
    if "optimizer" in table:
        table["optimizer"] = tf.keras.optimizers.serialize(table["optimizer"])

    return table


def deserialize_table_config(table_params: Dict[str, Any]) -> TableConfig:
    if "initializer" in table_params and table_params["initializer"]:
        table_params["initializer"] = tf.keras.initializers.deserialize(table_params["initializer"])
    if "optimizer" in table_params and table_params["optimizer"]:
        table_params["optimizer"] = tf.keras.optimizers.deserialize(table_params["optimizer"])
    table = TableConfig(**table_params)

    return table


def serialize_feature_config(feature_config: FeatureConfig) -> Dict[str, Any]:
    outputs = {}

    for key, val in feature_config.items():
        feature_config_dict = dict(name=val.name, max_sequence_length=val.max_sequence_length)
        feature_config_dict["table"] = serialize_table_config(feature_config_dict["table"])
        outputs[key] = feature_config_dict

    return outputs
