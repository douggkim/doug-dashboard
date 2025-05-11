"""Defines custom IOManagers for use with Dagster."""

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import deltalake
import fsspec
import pandas as pd
import polars as pl
from dagster import ConfigurableIOManager, InputContext, MetadataValue, OutputContext
from furl import furl
from loguru import logger


class GenericInputDeltaIOManager(ConfigurableIOManager, ABC):
    """A generic input IOManager that provides a standard implementation for writing data as a Delta Table.

    The input method is abstract and must be implemented by subclasses since different assets may require different
    DataFrame types.

    Attributes
    ----------
    storage_options : dict
        A dictionary of connection options used to connect to AWS S3 Storage.
    """

    storage_options: dict = dict()  # noqa: RUF012
    output_base_path: str

    @abstractmethod
    def load_input(self, context: InputContext) -> Any:
        """Load input data from the specified path."""

    def _get_metadata_values(self, context: InputContext | OutputContext, metadata_key: str) -> list[str]:
        """
        Extract certain metadata values from asset metadata.

        Parameters
        ----------
        context : InputContext or OutputContext
            The Dagster context object containing asset metadata

        Returns
        -------
        list[str]
            A list of the metadata values

        Examples
        --------
        >>> # With metadata containing provided metadata_key in metadata
        >>> _get_metadata_values(context_with_metadata)
        ["country", "brand_id", "date"]
        """
        asset_metadata = context.definition_metadata
        return asset_metadata.get(metadata_key, [])

    def _get_merge_predicates(self, primary_keys: list[str]) -> str:
        """
        Generate merge predicates for Delta table merge operations based on specified columns.

        Parameters
        ----------
        primary_keys : list[str]
            A list of column names to use as merge keys

        Returns
        -------
        str
            A string containing the merge predicate expression with source and target aliases

        Raises
        ------
        ValueError
            If primary_keys is empty or None

        Examples
        --------
        >>> _get_merge_predicates(["id", "region"])
        "s.id = t.id AND s.region = t.region"
        """
        if not primary_keys:
            raise ValueError("At least one column must be specified for merge operations")

        predicates = [f"s.{col} = t.{col}" for col in primary_keys]
        return " AND ".join(predicates)

    def _get_storage_path(self, context: InputContext | OutputContext) -> str:
        """
        Get the path for the Delta Table based on the asset identifier.

        Parameters
        ----------
        context : InputContext or OutputContext
            The Dagster context for the input or output operation.

        Returns
        -------
        str
            The path to the Delta Table
        """
        asset_identifiers = context.asset_key.parts
        # Concatenate asset parts to form the dataset name
        output_base_path = furl(self.output_base_path)
        for segment in asset_identifiers:
            output_base_path.path.add(segment)

        return str(output_base_path)

    def handle_output(self, context: OutputContext, obj: pl.LazyFrame | pl.DataFrame | pd.DataFrame) -> None:
        """Write a DataFrame to a specified path in AWS S3 storage as a Delta Table.

        Parameters
        ----------
        context : OutputContext
            The Dagster context for the output operation.
        obj : pl.LazyFrame | pl.DataFrame | pd.DataFrame
            The DataFrame to be written. It can be a polars LazyFrame, polars DataFrame, or pandas DataFrame.

        Raises
        ------
        TypeError
            Raised when the provided data is of an unsuported type.
        """
        if isinstance(obj, pl.LazyFrame):
            obj = obj.collect()
        elif isinstance(obj, pd.DataFrame):
            obj = pl.from_pandas(obj)
        elif not isinstance(obj, pl.DataFrame):
            raise TypeError("Unsupported object type. Must be a polars LazyFrame, DataFrame, or pandas DataFrame.")

        # Save metadata
        with pl.Config(tbl_formatting="MARKDOWN", tbl_hide_column_data_types=True, tbl_hide_dataframe_shape=True):
            metadata = {
                "df": MetadataValue.md(repr(obj.head())),
                "dagster/row_count": obj.shape[0],
                "n_cols": obj.shape[1],
            }
            context.add_output_metadata(metadata)

        # Save the DataFrame to the specified path
        path = self._get_storage_path(context)

        context.log.debug(
            f"Dumping to AWS S3 storage using the following storage options: {self.storage_options} and file path: {path}"
        )

        is_delta = deltalake.DeltaTable.is_deltatable(table_uri=f"{path}", storage_options=self.storage_options)
        primary_keys = self._get_metadata_values(context=context, metadata_key="primary_keys")
        partition_cols = self._get_metadata_values(context=context, metadata_key="partition_cols")

        if is_delta and primary_keys:
            merge_predicate = self._get_merge_predicates(primary_keys=primary_keys)

            context.log.info(f"Table at {path} exists, performing merge operation using columns:{primary_keys}")
            logger.info(f"Generated merge predicate for TABLE MERGE: {merge_predicate}")

            (
                obj.write_delta(
                    f"{path}",
                    mode="merge",
                    delta_merge_options={
                        "predicate": merge_predicate,
                        "source_alias": "s",
                        "target_alias": "t",
                    },
                    delta_write_options={"schema_mode": "overwrite"},
                    storage_options=self.storage_options,
                )
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute()
            )
        elif partition_cols:
            context.log.info(f"Writing to {path} - the table will be partitioned using columns: {partition_cols}")

            obj.write_delta(
                target=f"{path}",
                mode="overwrite",
                storage_options=self.storage_options,
                delta_write_options={"partition_by": partition_cols, "schema_mode": "overwrite"},
            )
        else:
            context.log.warning(
                f"Writing to {path} - No partition keys have been set. The table will be fully overwritten"
            )
            obj.write_delta(
                target=f"{path}",
                mode="overwrite",
                storage_options=self.storage_options,
                delta_write_options={"schema_mode": "overwrite"},
            )


class PandasDeltaIOManager(GenericInputDeltaIOManager):
    """An IOManager for loading and saving pandas DataFrames."""

    def load_input(self, context: InputContext) -> pd.DataFrame:
        """Load input data as a Delta Table from the specified path.

        Parameters
        ----------
        context : InputContext
            The Dagster context for the input operation.

        Returns
        -------
        pd.DataFrame
            The loaded DataFrame.
        """
        path = self._get_storage_path(context)

        return (
            pl.scan_delta(
                f"{path}",
                storage_options=self.storage_options,
            )
            .collect()
            .to_pandas()
        )


class PolarsLazyDeltaIOManager(GenericInputDeltaIOManager):
    """An IOManager for loading and saving polars LazyFrames."""

    def load_input(self, context: InputContext) -> pl.LazyFrame:
        """Load input data as a Delta Table from the specified path.

        Parameters
        ----------
        context : InputContext
            The Dagster context for the input operation.

        Returns
        -------
        pl.LazyFrame
            The loaded LazyFrame.
        """
        path = self._get_storage_path(context)
        return pl.scan_delta(
            f"{path}",
            storage_options=self.storage_options,
        )


class PolarsDeltaIOManager(GenericInputDeltaIOManager):
    """An IOManager for loading and saving polars DataFrames."""

    def load_input(self, context: InputContext) -> pl.DataFrame:
        """Load input data as a Delta Table from the specified path.

        Parameters
        ----------
        context : InputContext
            The Dagster context for the input operation.

        Returns
        -------
        pl.DataFrame
            The loaded DataFrame.
        """
        path = self._get_storage_path(context)
        return pl.scan_delta(
            f"{path}",
            storage_options=self.storage_options,
        ).collect()


class GenericJSONIOManager(ConfigurableIOManager, ABC):
    """A generic IO Manager that provides directory-based file operations for JSON data.

    This manager can read from and write to directories of JSON files, with support
    for different storage systems through fsspec.

    Attributes
    ----------
    storage_options : dict
        Options passed to fsspec when accessing storage
    output_base_path : str
        Base path where data will be stored
    """

    storage_options: dict = dict()  # noqa: RUF012
    output_base_path: str

    @abstractmethod
    def load_input(self, context: InputContext) -> Any:
        """Load input data from the specified directory path."""

    def _get_directory_path(self, context: InputContext | OutputContext) -> str:
        """
        Get the directory path for storage based on the asset identifier.

        Parameters
        ----------
        context : InputContext or OutputContext
            The Dagster context for the input or output operation.

        Returns
        -------
        str
            The path to the directory
        """
        asset_identifiers = context.asset_key.parts
        # Concatenate asset parts to form the directory path
        output_base_path = furl(self.output_base_path)

        for segment in asset_identifiers:
            output_base_path.path.add(segment)

        input_asset_partition_keys = sorted(context.asset_partition_keys)

        logger.info(f"input asset partition keys: {input_asset_partition_keys}")

        formatted_partition = input_asset_partition_keys[-1].replace("-", "_")
        output_base_path.path.add(formatted_partition)

        logger.info(f"File path for {asset_identifiers} is {output_base_path!s}")

        return str(output_base_path)

    def _get_fs(self, path: str) -> fsspec.AbstractFileSystem:
        """
        Get the appropriate filesystem for a given path.

        Parameters
        ----------
        path : str
            The path to get a filesystem for

        Returns
        -------
        fsspec.AbstractFileSystem
            The filesystem object for the given path
        """
        parsed = urlparse(path)

        # Use local filesystem for file:// or empty scheme
        if parsed.scheme in {"", "file"}:
            return fsspec.filesystem("file")
        # Use the appropriate filesystem with storage options
        return fsspec.filesystem(parsed.scheme, **self.storage_options)

    def _list_directory_files(self, path: str, pattern: str = "*") -> list[str]:
        """
        List all files in a directory matching the pattern.

        Parameters
        ----------
        path : str
            Directory path to list files from
        pattern : str
            Glob pattern to match files, default is "*"

        Returns
        -------
        list[str]
            List of file paths
        """
        fs = self._get_fs(path)

        # Some fsspec implementations don't support glob with absolute paths
        if fs.protocol == "file":
            # For local filesystem, use Path for more reliable globbing
            return [str(p) for p in Path(path).glob(pattern)]
        # For remote filesystems, use fs.glob
        if not fs.exists(path):
            return []
        return fs.glob(f"{path}/{pattern}")


class JSONTextIOManager(GenericJSONIOManager):
    """IO Manager that handles directory-based JSON file operations.

    This manager handles JSON files for the bronze layer and DataFrame files for the silver layer,
    working with any storage system supported by fsspec.
    """

    def handle_output(self, context: OutputContext, obj: dict | pl.DataFrame | pd.DataFrame) -> None:
        """
        Write output data to the appropriate location.

        Parameters
        ----------
        context : OutputContext
            The Dagster context for the output operation
        obj : Union[dict, pl.DataFrame, pd.DataFrame]
            The data to write, either a JSON-serializable dict or a DataFrame

        Raises
        ------
        TypeError
            If the object type is not supported
        """
        directory_path = self._get_directory_path(context)
        fs = self._get_fs(directory_path)

        # Create directory if it doesn't exist
        if not fs.exists(directory_path):
            fs.makedirs(directory_path, exist_ok=True)

        now = datetime.now(UTC)
        string_date = now.strftime("%Y%m%d_%H%M")

        # Handle different object types
        if isinstance(obj, dict):
            # Use current timestamp for filename
            file_name = f"{string_date}.json"
            file_path = f"{directory_path}/{file_name}"

            # Save JSON data
            with fs.open(file_path, "w") as f:
                json.dump(obj, f, indent=2)

            context.add_output_metadata({
                "file_path": MetadataValue.text(file_path),
                "timestamp": now.strftime("%Y%m%d_%H%M"),
            })
        elif isinstance(obj, str):
            # For text data
            # Use current timestamp for filename
            file_name = f"{string_date}.json"
            file_path = f"{directory_path}/{file_name}"

            # Save text data
            with fs.open(file_path, "w") as f:
                f.write(obj)

            context.add_output_metadata({
                "file_path": MetadataValue.text(file_path),
                "file_type": "text",
                "timestamp": now.strftime("%Y%m%d_%H%M"),
            })

        else:
            raise TypeError("Unsupported object type. Must be a dict or string.")

    def load_input(self, context: InputContext) -> list[dict | str]:
        """
        Load input data from the specified directory, automatically detecting file types.

        Parameters
        ----------
        context : InputContext
            The Dagster context for the input operation

        Returns
        -------
        list[dict | str]
            list of objects from all files.
            JSON files are loaded as dictionaries, text files as strings.

        Raises
        ------
        ValueError
            If no files are found or the data cannot be loaded
        """
        directory_path = self._get_directory_path(context)
        fs = self._get_fs(directory_path)

        # Check if directory exists
        if not fs.exists(directory_path):
            raise ValueError(f"Directory not found: {directory_path}")

        # Find all files in the directory that we can process
        all_files = []
        all_files.extend(self._list_directory_files(directory_path, "*.json"))
        all_files.extend(self._list_directory_files(directory_path, "*.txt"))

        if not all_files:
            raise ValueError(f"No compatible files found in directory: {directory_path}")

        # Process each file according to its extension
        result = []
        for file_path in all_files:
            file_extension = Path(file_path).suffix.lower()

            if file_extension == ".json":
                with fs.open(file_path, "r") as f:
                    result.append(json.load(f))
                    context.log.debug(f"Loaded JSON file: {file_path}")
            elif file_extension == ".txt":
                with fs.open(file_path, "r") as f:
                    result.append(f.read())
                    context.log.debug(f"Loaded text file: {file_path}")
            else:
                context.log.warning(f"Skipping unsupported file type: {file_path}")

        return result
