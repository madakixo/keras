# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Python-based idempotent model-saving functionality."""

import datetime
import io
import json
import tempfile
import threading
import warnings
import zipfile

import numpy as np
import tensorflow.compat.v2 as tf

import keras
from keras import losses
from keras.engine import base_layer
from keras.optimizers.optimizer_experimental import optimizer
from keras.saving.experimental.serialization_lib import deserialize_keras_object
from keras.saving.experimental.serialization_lib import serialize_keras_object
from keras.utils import generic_utils
from keras.utils import io_utils

try:
    import h5py
except ImportError:
    h5py = None

# isort: off

_CONFIG_FILENAME = "config.json"
_METADATA_FILENAME = "metadata.json"
_VARS_FNAME = "variables.weights"  # Will become e.g. "variables.weights.h5"
_ASSETS_DIRNAME = "assets"

# A temporary flag to enable the new idempotent saving framework.
_SAVING_V3_ENABLED = threading.local()
_SAVING_V3_ENABLED.value = False

ATTR_SKIPLIST = frozenset(
    {
        "_callable_losses",
        "_captured_weight_regularizer",
        "_checkpoint_dependencies",
        "_deferred_dependencies",
        "_eager_losses",
        "_inbound_nodes",
        "_inbound_nodes_value",
        "_output_layers",
        "_input_layers",
        "_keras_api_names",
        "_keras_api_names_v1",
        "_name_based_restores",
        "_non_trainable_weights",
        "_outbound_nodes",
        "_outbound_nodes_value",
        "_saved_model_arg_spec",
        "_self_name_based_restores",
        "_self_saveable_object_factories",
        "_self_tracked_trackables",
        "_self_unconditional_checkpoint_dependencies",
        "_self_unconditional_deferred_dependencies",
        "_self_unconditional_dependency_names",
        "_tf_api_names",
        "_tf_api_names_v1",
        "_trainable_weights",
        "_non_trainable_weights",
        "_unconditional_checkpoint_dependencies",
        "_unconditional_dependency_names",
        "_updates",
        "inbound_nodes",
        "submodules",
        "weights",
        "non_trainable_weights",
        "trainable_weights",
        "variables",
        "non_trainable_variables",
        "trainable_variables",
        "updates",  # Would raise a warning if visited.
        "state_updates",  # Would raise a warning if visited.
    }
)


def save_model(model, filepath, weights_format="h5"):
    """Save a zip-archive representing a Keras model to the given filepath.

    The zip-based archive contains the following structure:

    - JSON-based configuration file (config.json): Records of model, layer, and
        other trackables' configuration.
    - NPZ-based trackable state files, found in respective directories, such as
        model/states.npz, model/dense_layer/states.npz, etc.
    - Metadata file.

    The states of Keras trackables (layers, optimizers, loss, and metrics) are
    automatically saved as long as they can be discovered through the attributes
    returned by `dir(Model)`. Typically, the state includes the variables
    associated with the trackable, but some specially purposed layers may
    contain more such as the vocabularies stored in the hashmaps. The trackables
    define how their states are saved by exposing `save_state()` and
    `load_state()` APIs.

    For the case of layer states, the variables will be visited as long as
    they are either 1) referenced via layer attributes, or 2) referenced via a
    container (list, tuple, or dict), and the container is referenced via a
    layer attribute.
    """
    if not filepath.endswith(".keras"):
        raise ValueError(
            "Invalid `filepath` argument: expected a `.keras` extension. "
            f"Received: filepath={filepath}"
        )
    if weights_format == "h5" and h5py is None:
        raise ImportError("h5py must be installed in order to save a model.")

    if not model.built:
        warnings.warn(
            "You are saving a model that has not yet been built. "
            "It might not contain any weights yet. "
            "Consider building the model first by calling it "
            "on some data.",
            stacklevel=2,
        )
    saving_v3_enabled_value = getattr(_SAVING_V3_ENABLED, "value", False)
    _SAVING_V3_ENABLED.value = True

    serialized_model_dict = serialize_keras_object(model)
    config_json = json.dumps(serialized_model_dict)
    metadata_json = json.dumps(
        {
            "keras_version": keras.__version__,
            "date_saved": datetime.datetime.now().strftime("%Y-%m-%d@%H:%M:%S"),
        }
    )
    try:
        with zipfile.ZipFile(filepath, "w") as zf:

            with zf.open(_METADATA_FILENAME, "w") as f:
                f.write(metadata_json.encode())
            with zf.open(_CONFIG_FILENAME, "w") as f:
                f.write(config_json.encode())

            if weights_format == "h5":
                weights_store = H5IOStore(
                    _VARS_FNAME + ".h5", archive=zf, mode="w"
                )
            elif weights_format == "npz":
                weights_store = NpzIOStore(
                    _VARS_FNAME + ".npz", archive=zf, mode="w"
                )
            else:
                raise ValueError(
                    "Unknown `weights_format` argument. "
                    "Expected 'h5' or 'npz'. "
                    f"Received: weights_format={weights_format}"
                )

            asset_store = DiskIOStore(_ASSETS_DIRNAME, archive=zf, mode="w")

            _save_state(
                model,
                weights_store=weights_store,
                assets_store=asset_store,
                inner_path="",
                visited_trackables=set(),
            )
            weights_store.close()
            asset_store.close()

    except Exception as e:
        raise e
    finally:
        _SAVING_V3_ENABLED.value = saving_v3_enabled_value


def load_model(filepath, custom_objects=None, compile=True):
    """Load a zip archive representing a Keras model."""
    if not filepath.endswith(".keras"):
        raise ValueError(
            "Invalid filename: expected a `.keras` extension. "
            f"Received: filepath={filepath}"
        )

    saving_v3_enabled_value = getattr(_SAVING_V3_ENABLED, "value", False)
    _SAVING_V3_ENABLED.value = True

    try:
        with zipfile.ZipFile(filepath, "r") as zf:

            with zf.open(_CONFIG_FILENAME, "r") as f:
                config_json = f.read()

            # Note: we should NOT use a custom JSON decoder. Anything that
            # needs custom decoding must be handled in deserialize_keras_object.
            config_dict = json.loads(config_json)
            if not compile:
                # Disable compilation
                config_dict["config"]["compile_config"] = None
            # Construct the model from the configuration file in the archive.
            model = deserialize_keras_object(config_dict, custom_objects)

            all_filenames = zf.namelist()
            if _VARS_FNAME + ".h5" in all_filenames:
                weights_store = H5IOStore(
                    _VARS_FNAME + ".h5", archive=zf, mode="r"
                )
            elif _VARS_FNAME + ".npz" in all_filenames:
                weights_store = NpzIOStore(
                    _VARS_FNAME + ".npz", archive=zf, mode="r"
                )
            else:
                raise ValueError(
                    f"Expected a {_VARS_FNAME}.h5 or {_VARS_FNAME}.npz file."
                )

            if _ASSETS_DIRNAME in all_filenames:
                asset_store = DiskIOStore(_ASSETS_DIRNAME, archive=zf, mode="r")
            else:
                asset_store = None

            _load_state(
                model,
                weights_store=weights_store,
                assets_store=asset_store,
                inner_path="",
                visited_trackables=set(),
            )
            weights_store.close()
            if asset_store:
                asset_store.close()

    except Exception as e:
        raise e
    else:
        return model
    finally:
        _SAVING_V3_ENABLED.value = saving_v3_enabled_value


def save_weights_only(model, filepath):
    """Save only the weights of a model to a target filepath (.weights.h5).

    Note: only supports h5 for now.
    """
    # TODO: if h5 filepath is remote, create the file in a temporary directory
    # then upload it
    if not filepath.endswith(".weights.h5"):
        raise ValueError(
            "Invalid `filepath` argument: expected a `.weights.h5` extension. "
            f"Received: filepath={filepath}"
        )
    weights_store = H5IOStore(filepath, mode="w")
    _save_state(
        model,
        weights_store=weights_store,
        assets_store=None,
        inner_path="",
        visited_trackables=set(),
    )
    weights_store.close()


def load_weights_only(model, filepath):
    """Load the weights of a model from a filepath (.keras or .weights.h5).

    Note: only supports h5 for now.
    """
    temp_dir = None
    archive = None
    if filepath.endswith(".weights.h5"):
        # TODO: download file if h5 filepath is remote
        weights_store = H5IOStore(filepath, mode="r")
    elif filepath.endswith(".keras"):
        archive = zipfile.ZipFile(filepath, "r")
        weights_store = H5IOStore(
            _VARS_FNAME + ".h5", archive=archive, mode="r"
        )

    _load_state(
        model,
        weights_store=weights_store,
        assets_store=None,
        inner_path="",
        visited_trackables=set(),
    )
    weights_store.close()
    if temp_dir and tf.io.gfile.exists(temp_dir):
        tf.io.gfile.rmtree(temp_dir)
    if archive:
        archive.close()


def _write_to_zip_recursively(zipfile_to_save, system_path, zip_path):
    if not tf.io.gfile.isdir(system_path):
        zipfile_to_save.write(system_path, zip_path)
    else:
        for file_name in tf.io.gfile.listdir(system_path):
            system_file_path = tf.io.gfile.join(system_path, file_name)
            zip_file_path = tf.io.gfile.join(zip_path, file_name)
            _write_to_zip_recursively(
                zipfile_to_save, system_file_path, zip_file_path
            )


def _save_state(
    trackable, weights_store, assets_store, inner_path, visited_trackables
):
    # If the trackable has already been saved, skip it.
    if id(trackable) in visited_trackables:
        return

    # TODO(fchollet): better name?
    if hasattr(trackable, "_save_own_variables") and weights_store:
        trackable._save_own_variables(weights_store.make(inner_path))
    if hasattr(trackable, "_save_assets") and assets_store:
        trackable._save_assets(assets_store.make(inner_path))

    visited_trackables.add(id(trackable))

    # Recursively save state of children trackables (layers, optimizers, etc.)
    for child_attr in dir(trackable):
        if child_attr.startswith("__") or child_attr in ATTR_SKIPLIST:
            continue
        try:
            child_obj = getattr(trackable, child_attr)
        except Exception:
            # Avoid raising the exception when visiting the attributes.
            continue
        if _is_keras_trackable(child_obj):
            _save_state(
                child_obj,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, child_attr),
                visited_trackables=visited_trackables,
            )
        elif isinstance(child_obj, (list, dict, tuple, set)):
            _save_container_state(
                child_obj,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, child_attr),
                visited_trackables=visited_trackables,
            )


def _load_state(
    trackable, weights_store, assets_store, inner_path, visited_trackables
):
    if id(trackable) in visited_trackables:
        return

    if hasattr(trackable, "_load_own_variables") and weights_store:
        trackable._load_own_variables(weights_store.get(inner_path))
    if hasattr(trackable, "_load_assets") and assets_store:
        trackable._load_assets(assets_store.get(inner_path))

    visited_trackables.add(id(trackable))

    # Recursively load states for Keras trackables such as layers/optimizers.
    for child_attr in dir(trackable):
        if child_attr.startswith("__") or child_attr in ATTR_SKIPLIST:
            continue
        try:
            child_obj = getattr(trackable, child_attr)
        except Exception:
            # Avoid raising exceptions when visiting attributes.
            continue
        if _is_keras_trackable(child_obj):
            _load_state(
                child_obj,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, child_attr),
                visited_trackables=visited_trackables,
            )
        elif isinstance(child_obj, (list, dict, tuple, set)):
            _load_container_state(
                child_obj,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, child_attr),
                visited_trackables=visited_trackables,
            )


def _save_container_state(
    container, weights_store, assets_store, inner_path, visited_trackables
):
    used_names = {}
    for trackable in container:
        if _is_keras_trackable(trackable):
            # Do NOT address the trackable via `trackable.name`, since
            # names are usually autogenerated and thus not reproducible
            # (i.e. they may vary across two instances of the same model).
            name = generic_utils.to_snake_case(trackable.__class__.__name__)
            if name in used_names:
                used_names[name] += 1
                name = f"{name}_{used_names[name]}"
            else:
                used_names[name] = 0
            _save_state(
                trackable,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, name),
                visited_trackables=visited_trackables,
            )


def _load_container_state(
    container, weights_store, assets_store, inner_path, visited_trackables
):
    used_names = {}
    for trackable in container:
        if _is_keras_trackable(trackable):
            name = generic_utils.to_snake_case(trackable.__class__.__name__)
            if name in used_names:
                used_names[name] += 1
                name = f"{name}_{used_names[name]}"
            else:
                used_names[name] = 0
            _load_state(
                trackable,
                weights_store,
                assets_store,
                inner_path=tf.io.gfile.join(inner_path, name),
                visited_trackables=visited_trackables,
            )


class DiskIOStore:
    """Asset store backed by disk storage.

    If `archive` is specified, then `root_path` refers to the filename
    inside the archive.

    If `archive` is not specified, then `root_path` refers to the full path of
    the target directory.
    """

    def __init__(self, root_path, archive=None, mode=None):
        self.mode = mode
        self.root_path = root_path
        self.archive = archive
        self.tmp_dir = None
        if self.archive:
            self.tmp_dir = _get_temp_dir()
            if self.mode == "r":
                self.archive.extract(root_path, path=self.tmp_dir)
            self.working_dir = self.tmp_dir
        else:
            if mode == "r":
                self.working_dir = root_path
            else:
                self.tmp_dir = _get_temp_dir()
                self.working_dir = self.tmp_dir

    def make(self, path):
        if not path:
            return self.tmp_dir
        path = tf.io.gfile.join(self.tmp_dir, path)
        if not tf.io.gfile.exists(path):
            tf.io.gfile.makedirs(path)
        return path

    def get(self, path):
        if not path:
            return self.tmp_dir
        path = tf.io.gfile.join(self.tmp_dir, path)
        if tf.io.gfile.exists(path):
            return path
        return None

    def close(self):
        if self.mode == "w" and self.archive:
            _write_to_zip_recursively(
                self.archive, self.tmp_dir, self.root_path
            )
        if self.tmp_dir and tf.io.gfile.exists(self.tmp_dir):
            tf.io.gfile.rmtree(self.tmp_dir)


class H5IOStore:
    def __init__(self, root_path, archive=None, mode="r"):
        """Numerical variable store backed by HDF5.

        If `archive` is specified, then `root_path` refers to the filename
        inside the archive.

        If `archive` is not specified, then `root_path` refers to the path of
        the h5 file on disk.
        """
        self.root_path = root_path
        self.mode = mode
        self.archive = archive
        self.io_file = None

        if self.archive:
            if self.mode == "w":
                self.io_file = io.BytesIO()
            else:
                self.io_file = self.archive.open(self.root_path, "r")
            self.h5_file = h5py.File(self.io_file, mode=self.mode)
        else:
            self.h5_file = h5py.File(root_path, mode=self.mode)

    def make(self, path):
        if not path:
            return self.h5_file.create_group("vars")
        return self.h5_file.create_group(path).create_group("vars")

    def get(self, path):
        if not path:
            return self.h5_file["vars"]
        if path in self.h5_file and "vars" in self.h5_file[path]:
            return self.h5_file[path]["vars"]
        return {}

    def close(self):
        self.h5_file.close()
        if self.mode == "w" and self.archive:
            self.archive.writestr(self.root_path, self.io_file.getvalue())
        if self.io_file:
            self.io_file.close()


class NpzIOStore:
    def __init__(self, root_path, archive=None, mode="r"):
        """Numerical variable store backed by NumPy.savez/load.

         If `archive` is specified, then `root_path` refers to the filename
        inside the archive.

        If `archive` is not specified, then `root_path` refers to the path of
        the npz file on disk.
        """
        self.root_path = root_path
        self.mode = mode
        self.archive = archive
        if mode == "w":
            self.contents = {}
        else:
            if self.archive:
                self.f = archive.open(root_path, mode="r")
            else:
                self.f = open(root_path, mode="rb")
            self.contents = np.load(self.f, allow_pickle=True)

    def make(self, path):
        if not path:
            self.contents["__root__"] = {}
            return self.contents["__root__"]
        self.contents[path] = {}
        return self.contents[path]

    def get(self, path):
        if not path:
            if "__root__" in self.contents:
                return dict(self.contents["__root__"])
            return {}
        if path in self.contents:
            return self.contents[path].tolist()
        return {}

    def close(self):
        if self.mode == "w":
            if self.archive:
                self.f = self.archive.open(
                    self.root_path, mode="w", force_zip64=True
                )
            else:
                self.f = open(self.root_path, mode="wb")
            np.savez(self.f, **self.contents)
        self.f.close()


def _get_temp_dir():
    temp_dir = tempfile.mkdtemp()
    testfile = tempfile.TemporaryFile(dir=temp_dir)
    testfile.close()
    return temp_dir


def _is_keras_trackable(obj):
    from keras.metrics import base_metric  # To avoid circular import

    return isinstance(
        obj,
        (
            base_layer.Layer,
            optimizer.Optimizer,
            base_metric.Metric,
            losses.Loss,
        ),
    )


def saving_v3_enabled():
    return getattr(_SAVING_V3_ENABLED, "value", False)


# Some debugging utilities.


def _print_h5_file(h5_file, prefix="", action=None):
    if not prefix:
        print(f"Keras weights file ({h5_file}) {action}:")
    if not hasattr(h5_file, "keys"):
        return
    for key in h5_file.keys():
        print(f"...{prefix}{key}")
        _print_h5_file(h5_file[key], prefix=prefix + "...")


def _print_zip_file(zipfile, action):
    # TODO(fchollet): move to debugging logs.
    io_utils.print_msg(f"Keras model archive {action}:")
    # Same as `ZipFile.printdir()` except for using Keras' printing utility.
    io_utils.print_msg(
        "%-46s %19s %12s" % ("File Name", "Modified    ", "Size")
    )
    for zinfo in zipfile.filelist:
        date = "%d-%02d-%02d %02d:%02d:%02d" % zinfo.date_time[:6]
        io_utils.print_msg(
            "%-46s %s %12d" % (zinfo.filename, date, zinfo.file_size)
        )
