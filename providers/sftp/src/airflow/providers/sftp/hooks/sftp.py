#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""This module contains SFTP hook."""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import stat
import warnings
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

import asyncssh
from asgiref.sync import sync_to_async

from airflow.exceptions import AirflowException, AirflowProviderDeprecationWarning
from airflow.providers.sftp.version_compat import BaseHook
from airflow.providers.ssh.hooks.ssh import SSHHook

if TYPE_CHECKING:
    from paramiko import SSHClient
    from paramiko.sftp_attr import SFTPAttributes
    from paramiko.sftp_client import SFTPClient

    from airflow.models.connection import Connection


class SFTPHook(SSHHook):
    """
    Interact with SFTP.

    This hook inherits the SSH hook. Please refer to SSH hook for the input
    arguments.

    :Pitfalls::

        - In contrast with FTPHook describe_directory only returns size, type and
          modify. It doesn't return unix.owner, unix.mode, perm, unix.group and
          unique.
        - If no mode is passed to create_directory it will be created with 777
          permissions.

    Errors that may occur throughout but should be handled downstream.

    For consistency reasons with SSHHook, the preferred parameter is "ssh_conn_id".

    :param ssh_conn_id: The :ref:`sftp connection id<howto/connection:sftp>`
    """

    conn_name_attr = "ssh_conn_id"
    default_conn_name = "sftp_default"
    conn_type = "sftp"
    hook_name = "SFTP"

    @classmethod
    def get_ui_field_behaviour(cls) -> dict[str, Any]:
        return {
            "hidden_fields": ["schema"],
            "relabeling": {
                "login": "Username",
            },
        }

    def __init__(
        self,
        ssh_conn_id: str | None = "sftp_default",
        host_proxy_cmd: str | None = None,
        *args,
        **kwargs,
    ) -> None:
        self.conn: SFTPClient | None = None

        # TODO: remove support for ssh_hook when it is removed from SFTPOperator
        if kwargs.get("ssh_hook") is not None:
            warnings.warn(
                "Parameter `ssh_hook` is deprecated and will be ignored.",
                AirflowProviderDeprecationWarning,
                stacklevel=2,
            )

        ftp_conn_id = kwargs.pop("ftp_conn_id", None)
        if ftp_conn_id:
            warnings.warn(
                "Parameter `ftp_conn_id` is deprecated. Please use `ssh_conn_id` instead.",
                AirflowProviderDeprecationWarning,
                stacklevel=2,
            )
            ssh_conn_id = ftp_conn_id

        kwargs["ssh_conn_id"] = ssh_conn_id
        kwargs["host_proxy_cmd"] = host_proxy_cmd
        self.ssh_conn_id = ssh_conn_id

        self._ssh_conn: SSHClient | None = None
        self._sftp_conn: SFTPClient | None = None
        self._conn_count = 0

        super().__init__(*args, **kwargs)

    def get_conn(self) -> SFTPClient:  # type: ignore[override]
        """Open an SFTP connection to the remote host."""
        if self.conn is None:
            self.conn = super().get_conn().open_sftp()
        return self.conn

    def close_conn(self) -> None:
        """Close the SFTP connection."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @contextmanager
    def get_managed_conn(self) -> Generator[SFTPClient, None, None]:
        """Context manager that closes the connection after use."""
        if self._sftp_conn is None:
            ssh_conn: SSHClient = super().get_conn()
            self._ssh_conn = ssh_conn
            self._sftp_conn = ssh_conn.open_sftp()
        self._conn_count += 1

        try:
            yield self._sftp_conn
        finally:
            self._conn_count -= 1
            if self._conn_count == 0 and self._ssh_conn is not None and self._sftp_conn is not None:
                self._sftp_conn.close()
                self._sftp_conn = None
                self._ssh_conn.close()
                self._ssh_conn = None
                if hasattr(self, "host_proxy"):
                    del self.host_proxy

    def get_conn_count(self) -> int:
        """Get the number of open connections."""
        return self._conn_count

    def describe_directory(self, path: str) -> dict[str, dict[str, str | int | None]]:
        """
        Get file information in a directory on the remote system.

        The return format is ``{filename: {attributes}}``. The remote system
        support the MLSD command.

        :param path: full path to the remote directory
        """
        with self.get_managed_conn() as conn:  # type: SFTPClient
            flist = sorted(conn.listdir_attr(path), key=lambda x: x.filename)
            files = {}
            for f in flist:
                modify = datetime.datetime.fromtimestamp(f.st_mtime).strftime("%Y%m%d%H%M%S")  # type: ignore
                files[f.filename] = {
                    "size": f.st_size,
                    "type": "dir" if stat.S_ISDIR(f.st_mode) else "file",  # type: ignore
                    "modify": modify,
                }
            return files

    def list_directory(self, path: str) -> list[str]:
        """
        List files in a directory on the remote system.

        :param path: full path to the remote directory to list
        """
        with self.get_managed_conn() as conn:
            return sorted(conn.listdir(path))

    def list_directory_with_attr(self, path: str) -> list[SFTPAttributes]:
        """
        List files in a directory on the remote system including their SFTPAttributes.

        :param path: full path to the remote directory to list
        """
        with self.get_managed_conn() as conn:
            return [file for file in conn.listdir_attr(path)]

    def mkdir(self, path: str, mode: int = 0o777) -> None:
        """
        Create a directory on the remote system.

        The default mode is ``0o777``, but on some systems, the current umask
        value may be first masked out.

        :param path: full path to the remote directory to create
        :param mode: int permissions of octal mode for directory
        """
        with self.get_managed_conn() as conn:
            conn.mkdir(path, mode=mode)

    def isdir(self, path: str) -> bool:
        """
        Check if the path provided is a directory.

        :param path: full path to the remote directory to check
        """
        with self.get_managed_conn() as conn:
            try:
                return stat.S_ISDIR(conn.stat(path).st_mode)  # type: ignore
            except OSError:
                return False

    def isfile(self, path: str) -> bool:
        """
        Check if the path provided is a file.

        :param path: full path to the remote file to check
        """
        with self.get_managed_conn() as conn:
            try:
                return stat.S_ISREG(conn.stat(path).st_mode)  # type: ignore
            except OSError:
                return False

    def create_directory(self, path: str, mode: int = 0o777) -> None:
        """
        Create a directory on the remote system.

        The default mode is ``0o777``, but on some systems, the current umask
        value may be first masked out. Different from :func:`.mkdir`, this
        function attempts to create parent directories if needed, and returns
        silently if the target directory already exists.

        :param path: full path to the remote directory to create
        :param mode: int permissions of octal mode for directory
        """
        if self.isdir(path):
            self.log.info("%s already exists", path)
            return
        if self.isfile(path):
            raise AirflowException(f"{path} already exists and is a file")
        dirname, basename = os.path.split(path)
        if dirname and not self.isdir(dirname):
            self.create_directory(dirname, mode)
        if basename:
            self.log.info("Creating %s", path)
            with self.get_managed_conn() as conn:
                conn.mkdir(path, mode=mode)

    def delete_directory(self, path: str, include_files: bool = False) -> None:
        """
        Delete a directory on the remote system.

        :param path: full path to the remote directory to delete
        """
        files: list[str] = []
        dirs: list[str] = []

        if include_files is True:
            files, dirs, _ = self.get_tree_map(path)
            dirs = dirs[::-1]  # reverse the order for deleting deepest directories first

        with self.get_managed_conn() as conn:
            for file_path in files:
                conn.remove(file_path)
            for dir_path in dirs:
                conn.rmdir(dir_path)
            conn.rmdir(path)

    def retrieve_file(self, remote_full_path: str, local_full_path: str, prefetch: bool = True) -> None:
        """
        Transfer the remote file to a local location.

        If local_full_path is a string path, the file will be put
        at that location.

        :param remote_full_path: full path to the remote file
        :param local_full_path: full path to the local file or a file-like buffer
        :param prefetch: controls whether prefetch is performed (default: True)
        """
        with self.get_managed_conn() as conn:
            if isinstance(local_full_path, BytesIO):
                # It's a file-like object ( BytesIO), so use getfo().
                self.log.info("Using streaming download for %s", remote_full_path)
                conn.getfo(remote_full_path, local_full_path, prefetch=prefetch)
            # We use hasattr checking for 'write' for cases like google.cloud.storage.fileio.BlobWriter
            elif hasattr(local_full_path, "write"):
                self.log.info("Using streaming download for %s", remote_full_path)
                # We need to cast to pass pre-commit checks
                stream_full_path = cast("IO[bytes]", local_full_path)
                conn.getfo(remote_full_path, stream_full_path, prefetch=prefetch)
            elif isinstance(local_full_path, (str, bytes, os.PathLike)):
                # It's a string path, so use get().
                self.log.info("Using standard file download for %s", remote_full_path)
                conn.get(remote_full_path, local_full_path, prefetch=prefetch)
            # If it's neither, it's an unsupported type.
            else:
                raise TypeError(
                    f"Unsupported type for local_full_path: {type(local_full_path)}. "
                    "Expected a stream-like object or a path-like object."
                )

    def store_file(self, remote_full_path: str, local_full_path: str, confirm: bool = True) -> None:
        """
        Transfer a local file to the remote location.

        If local_full_path_or_buffer is a string path, the file will be read
        from that location.

        :param remote_full_path: full path to the remote file
        :param local_full_path: full path to the local file or a file-like buffer
        """
        with self.get_managed_conn() as conn:
            if isinstance(local_full_path, BytesIO):
                conn.putfo(local_full_path, remote_full_path, confirm=confirm)
            else:
                conn.put(local_full_path, remote_full_path, confirm=confirm)

    def delete_file(self, path: str) -> None:
        """
        Remove a file on the server.

        :param path: full path to the remote file
        """
        with self.get_managed_conn() as conn:
            conn.remove(path)

    def retrieve_directory(self, remote_full_path: str, local_full_path: str, prefetch: bool = True) -> None:
        """
        Transfer the remote directory to a local location.

        If local_full_path is a string path, the directory will be put
        at that location.

        :param remote_full_path: full path to the remote directory
        :param local_full_path: full path to the local directory
        :param prefetch: controls whether prefetch is performed (default: True)
        """
        if Path(local_full_path).exists():
            raise AirflowException(f"{local_full_path} already exists")
        Path(local_full_path).mkdir(parents=True)
        with self.get_managed_conn():
            files, dirs, _ = self.get_tree_map(remote_full_path)
            for dir_path in dirs:
                new_local_path = os.path.join(local_full_path, os.path.relpath(dir_path, remote_full_path))
                Path(new_local_path).mkdir(parents=True, exist_ok=True)
            for file_path in files:
                new_local_path = os.path.join(local_full_path, os.path.relpath(file_path, remote_full_path))
                self.retrieve_file(file_path, new_local_path, prefetch)

    def retrieve_directory_concurrently(
        self,
        remote_full_path: str,
        local_full_path: str,
        workers: int = os.cpu_count() or 2,
        prefetch: bool = True,
    ) -> None:
        """
        Transfer the remote directory to a local location concurrently.

        If local_full_path is a string path, the directory will be put
        at that location.

        :param remote_full_path: full path to the remote directory
        :param local_full_path: full path to the local directory
        :param prefetch: controls whether prefetch is performed (default: True)
        :param workers: number of workers to use for concurrent transfer (default: number of CPUs or 2 if undetermined)
        """

        def retrieve_file_chunk(
            conn: SFTPClient, local_file_chunk: list[str], remote_file_chunk: list[str], prefetch: bool = True
        ):
            for local_file, remote_file in zip(local_file_chunk, remote_file_chunk):
                conn.get(remote_file, local_file, prefetch=prefetch)

        with self.get_managed_conn():
            if Path(local_full_path).exists():
                raise AirflowException(f"{local_full_path} already exists")
            Path(local_full_path).mkdir(parents=True)
            new_local_file_paths, remote_file_paths = [], []
            files, dirs, _ = self.get_tree_map(remote_full_path)
            for dir_path in dirs:
                new_local_path = os.path.join(local_full_path, os.path.relpath(dir_path, remote_full_path))
                Path(new_local_path).mkdir(parents=True, exist_ok=True)
            for file in files:
                remote_file_paths.append(file)
                new_local_file_paths.append(
                    os.path.join(local_full_path, os.path.relpath(file, remote_full_path))
                )
        remote_file_chunks = [remote_file_paths[i::workers] for i in range(workers)]
        local_file_chunks = [new_local_file_paths[i::workers] for i in range(workers)]
        self.log.info("Opening %s new SFTP connections", workers)
        conns = [SFTPHook(ssh_conn_id=self.ssh_conn_id).get_conn() for _ in range(workers)]
        try:
            self.log.info("Retrieving files concurrently with %s threads", workers)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        retrieve_file_chunk,
                        conns[i],
                        local_file_chunks[i],
                        remote_file_chunks[i],
                        prefetch,
                    )
                    for i in range(workers)
                ]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        finally:
            for conn in conns:
                conn.close()

    def store_directory(self, remote_full_path: str, local_full_path: str, confirm: bool = True) -> None:
        """
        Transfer a local directory to the remote location.

        If local_full_path is a string path, the directory will be read
        from that location.

        :param remote_full_path: full path to the remote directory
        :param local_full_path: full path to the local directory
        """
        if self.path_exists(remote_full_path):
            raise AirflowException(f"{remote_full_path} already exists")
        with self.get_managed_conn():
            self.create_directory(remote_full_path)
            for root, dirs, files in os.walk(local_full_path):
                for dir_name in dirs:
                    dir_path = os.path.join(root, dir_name)
                    new_remote_path = os.path.join(
                        remote_full_path, os.path.relpath(dir_path, local_full_path)
                    )
                    self.create_directory(new_remote_path)
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    new_remote_path = os.path.join(
                        remote_full_path, os.path.relpath(file_path, local_full_path)
                    )
                    self.store_file(new_remote_path, file_path, confirm)

    def store_directory_concurrently(
        self,
        remote_full_path: str,
        local_full_path: str,
        confirm: bool = True,
        workers: int = os.cpu_count() or 2,
    ) -> None:
        """
        Transfer a local directory to the remote location concurrently.

        If local_full_path is a string path, the directory will be read
        from that location.

        :param remote_full_path: full path to the remote directory
        :param local_full_path: full path to the local directory
        :param confirm: whether to confirm the file size after transfer (default: True)
        :param workers: number of workers to use for concurrent transfer (default: number of CPUs or 2 if undetermined)
        """

        def store_file_chunk(
            conn: SFTPClient, local_file_chunk: list[str], remote_file_chunk: list[str], confirm: bool
        ):
            for local_file, remote_file in zip(local_file_chunk, remote_file_chunk):
                conn.put(local_file, remote_file, confirm=confirm)

        with self.get_managed_conn():
            if self.path_exists(remote_full_path):
                raise AirflowException(f"{remote_full_path} already exists")
            self.create_directory(remote_full_path)

            local_file_paths, new_remote_file_paths = [], []
            for root, dirs, files in os.walk(local_full_path):
                for dir_name in dirs:
                    dir_path = os.path.join(root, dir_name)
                    new_remote_path = os.path.join(
                        remote_full_path, os.path.relpath(dir_path, local_full_path)
                    )
                    self.create_directory(new_remote_path)
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    new_remote_path = os.path.join(
                        remote_full_path, os.path.relpath(file_path, local_full_path)
                    )
                    local_file_paths.append(file_path)
                    new_remote_file_paths.append(new_remote_path)

        remote_file_chunks = [new_remote_file_paths[i::workers] for i in range(workers)]
        local_file_chunks = [local_file_paths[i::workers] for i in range(workers)]
        self.log.info("Opening %s new SFTP connections", workers)
        conns = [SFTPHook(ssh_conn_id=self.ssh_conn_id).get_conn() for _ in range(workers)]
        try:
            self.log.info("Storing files concurrently with %s threads", workers)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        store_file_chunk, conns[i], local_file_chunks[i], remote_file_chunks[i], confirm
                    )
                    for i in range(workers)
                ]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        finally:
            for conn in conns:
                conn.close()

    def get_mod_time(self, path: str) -> str:
        """
        Get an entry's modification time.

        :param path: full path to the remote file
        """
        with self.get_managed_conn() as conn:
            ftp_mdtm = conn.stat(path).st_mtime
            return datetime.datetime.fromtimestamp(ftp_mdtm).strftime("%Y%m%d%H%M%S")  # type: ignore

    def path_exists(self, path: str) -> bool:
        """
        Whether a remote entity exists.

        :param path: full path to the remote file or directory
        """
        with self.get_managed_conn() as conn:
            try:
                conn.stat(path)
            except OSError:
                return False
            return True

    @staticmethod
    def _is_path_match(path: str, prefix: str | None = None, delimiter: str | None = None) -> bool:
        """
        Whether given path starts with ``prefix`` (if set) and ends with ``delimiter`` (if set).

        :param path: path to be checked
        :param prefix: if set path will be checked is starting with prefix
        :param delimiter: if set path will be checked is ending with suffix
        :return: bool
        """
        if prefix is not None and not path.startswith(prefix):
            return False
        if delimiter is not None and not path.endswith(delimiter):
            return False
        return True

    def walktree(
        self,
        path: str,
        fcallback: Callable[[str], Any | None],
        dcallback: Callable[[str], Any | None],
        ucallback: Callable[[str], Any | None],
        recurse: bool = True,
    ) -> None:
        """
        Recursively descend, depth first, the directory tree at ``path``.

        This calls discrete callback functions for each regular file, directory,
        and unknown file type.

        :param str path:
            root of remote directory to descend, use '.' to start at
            :attr:`.pwd`
        :param callable fcallback:
            callback function to invoke for a regular file.
            (form: ``func(str)``)
        :param callable dcallback:
            callback function to invoke for a directory. (form: ``func(str)``)
        :param callable ucallback:
            callback function to invoke for an unknown file type.
            (form: ``func(str)``)
        :param bool recurse: *Default: True* - should it recurse
        """
        for entry in self.list_directory_with_attr(path):
            pathname = os.path.join(path, entry.filename)
            mode = entry.st_mode
            if stat.S_ISDIR(mode):  # type: ignore
                # It's a directory, call the dcallback function
                dcallback(pathname)
                if recurse:
                    # now, recurse into it
                    self.walktree(pathname, fcallback, dcallback, ucallback)
            elif stat.S_ISREG(mode):  # type: ignore
                # It's a file, call the fcallback function
                fcallback(pathname)
            else:
                # Unknown file type
                ucallback(pathname)

    def get_tree_map(
        self, path: str, prefix: str | None = None, delimiter: str | None = None
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Get tuple with recursive lists of files, directories and unknown paths.

        It is possible to filter results by giving prefix and/or delimiter parameters.

        :param path: path from which tree will be built
        :param prefix: if set paths will be added if start with prefix
        :param delimiter: if set paths will be added if end with delimiter
        :return: tuple with list of files, dirs and unknown items
        """
        files: list[str] = []
        dirs: list[str] = []
        unknowns: list[str] = []

        def append_matching_path_callback(list_: list[str]) -> Callable:
            return lambda item: list_.append(item) if self._is_path_match(item, prefix, delimiter) else None

        self.walktree(
            path=path,
            fcallback=append_matching_path_callback(files),
            dcallback=append_matching_path_callback(dirs),
            ucallback=append_matching_path_callback(unknowns),
            recurse=True,
        )

        return files, dirs, unknowns

    def test_connection(self) -> tuple[bool, str]:
        """Test the SFTP connection by calling path with directory."""
        try:
            with self.get_managed_conn() as conn:
                conn.normalize(".")
                return True, "Connection successfully tested"
        except Exception as e:
            return False, str(e)

    def get_file_by_pattern(self, path, fnmatch_pattern) -> str:
        """
        Get the first matching file based on the given fnmatch type pattern.

        :param path: path to be checked
        :param fnmatch_pattern: The pattern that will be matched with `fnmatch`
        :return: string containing the first found file, or an empty string if none matched
        """
        for file in self.list_directory(path):
            if fnmatch(file, fnmatch_pattern):
                return file
        return ""

    def get_files_by_pattern(self, path, fnmatch_pattern) -> list[str]:
        """
        Get all matching files based on the given fnmatch type pattern.

        :param path: path to be checked
        :param fnmatch_pattern: The pattern that will be matched with `fnmatch`
        :return: list of string containing the found files, or an empty list if none matched
        """
        matched_files = []
        for file in self.list_directory_with_attr(path):
            if fnmatch(file.filename, fnmatch_pattern):
                matched_files.append(file.filename)

        return matched_files


class SFTPHookAsync(BaseHook):
    """
    Interact with an SFTP server via asyncssh package.

    :param sftp_conn_id: SFTP connection ID to be used for connecting to SFTP server
    :param host: hostname of the SFTP server
    :param port: port of the SFTP server
    :param username: username used when authenticating to the SFTP server
    :param password: password used when authenticating to the SFTP server.
        Can be left blank if using a key file
    :param known_hosts: path to the known_hosts file on the local file system. Defaults to ``~/.ssh/known_hosts``.
    :param key_file: path to the client key file used for authentication to SFTP server
    :param passphrase: passphrase used with the key_file for authentication to SFTP server
    """

    conn_name_attr = "ssh_conn_id"
    default_conn_name = "sftp_default"
    conn_type = "sftp"
    hook_name = "SFTP"
    default_known_hosts = "~/.ssh/known_hosts"

    def __init__(  # nosec: B107
        self,
        sftp_conn_id: str = default_conn_name,
        host: str = "",
        port: int = 22,
        username: str = "",
        password: str = "",
        known_hosts: str = default_known_hosts,
        key_file: str = "",
        passphrase: str = "",
        private_key: str = "",
    ) -> None:
        self.sftp_conn_id = sftp_conn_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.known_hosts: bytes | str = os.path.expanduser(known_hosts)
        self.key_file = key_file
        self.passphrase = passphrase
        self.private_key = private_key

    def _parse_extras(self, conn: Connection) -> None:
        """Parse extra fields from the connection into instance fields."""
        extra_options = conn.extra_dejson
        if "key_file" in extra_options and self.key_file == "":
            self.key_file = extra_options["key_file"]
        if "known_hosts" in extra_options and self.known_hosts != self.default_known_hosts:
            self.known_hosts = extra_options["known_hosts"]
        if ("passphrase" or "private_key_passphrase") in extra_options:
            self.passphrase = extra_options["passphrase"]
        if "private_key" in extra_options:
            self.private_key = extra_options["private_key"]

        host_key = extra_options.get("host_key")
        no_host_key_check = extra_options.get("no_host_key_check")

        if no_host_key_check is not None:
            no_host_key_check = str(no_host_key_check).lower() == "true"
            if host_key is not None and no_host_key_check:
                raise ValueError("Host key check was skipped, but `host_key` value was given")
            if no_host_key_check:
                self.log.warning(
                    "No Host Key Verification. This won't protect against Man-In-The-Middle attacks"
                )
                self.known_hosts = "none"

        if host_key is not None:
            self.known_hosts = f"{conn.host} {host_key}".encode()

    async def _get_conn(self) -> asyncssh.SSHClientConnection:
        """
        Asynchronously connect to the SFTP server as an SSH client.

        The following parameters are provided either in the extra json object in
        the SFTP connection definition

        - key_file
        - known_hosts
        - passphrase
        """
        conn = await sync_to_async(self.get_connection)(self.sftp_conn_id)
        if conn.extra is not None:
            self._parse_extras(conn)  # type: ignore[arg-type]

        conn_config: dict[str, Any] = {
            "host": conn.host,
            "port": conn.port,
            "username": conn.login,
            "password": conn.password,
        }
        if self.key_file:
            conn_config.update(client_keys=self.key_file)
        if self.known_hosts:
            if self.known_hosts.lower() == "none":
                conn_config.update(known_hosts=None)
            else:
                conn_config.update(known_hosts=self.known_hosts)
        if self.private_key:
            _private_key = asyncssh.import_private_key(self.private_key, self.passphrase)
            conn_config["client_keys"] = [_private_key]
        if self.passphrase:
            conn_config.update(passphrase=self.passphrase)
        ssh_client_conn = await asyncssh.connect(**conn_config)
        return ssh_client_conn

    async def list_directory(self, path: str = "") -> list[str] | None:  # type: ignore[return]
        """Return a list of files on the SFTP server at the provided path."""
        async with await self._get_conn() as ssh_conn:
            sftp_client = await ssh_conn.start_sftp_client()
            try:
                files = await sftp_client.listdir(path)
                return sorted(files)
            except asyncssh.SFTPNoSuchFile:
                return None

    async def read_directory(self, path: str = "") -> Sequence[asyncssh.sftp.SFTPName] | None:  # type: ignore[return]
        """Return a list of files along with their attributes on the SFTP server at the provided path."""
        async with await self._get_conn() as ssh_conn:
            sftp_client = await ssh_conn.start_sftp_client()
            try:
                return await sftp_client.readdir(path)
            except asyncssh.SFTPNoSuchFile:
                return None

    async def get_files_and_attrs_by_pattern(
        self, path: str = "", fnmatch_pattern: str = ""
    ) -> Sequence[asyncssh.sftp.SFTPName]:
        """
        Get the files along with their attributes matching the pattern (e.g. ``*.pdf``) at the provided path.

        if one exists. Otherwise, raises an AirflowException to be handled upstream for deferring
        """
        files_list = await self.read_directory(path)
        if files_list is None:
            raise FileNotFoundError(f"No files at path {path!r} found...")
        matched_files = [file for file in files_list if fnmatch(str(file.filename), fnmatch_pattern)]
        return matched_files

    async def get_mod_time(self, path: str) -> str:  # type: ignore[return]
        """
        Make SFTP async connection.

        Looks for last modified time in the specific file path and returns last modification time for
         the file path.

        :param path: full path to the remote file
        """
        async with await self._get_conn() as ssh_conn:
            try:
                sftp_client = await ssh_conn.start_sftp_client()
                ftp_mdtm = await sftp_client.stat(path)
                modified_time = ftp_mdtm.mtime
                mod_time = datetime.datetime.fromtimestamp(modified_time).strftime("%Y%m%d%H%M%S")  # type: ignore[arg-type]
                self.log.info("Found File %s last modified: %s", str(path), str(mod_time))
                return mod_time
            except asyncssh.SFTPNoSuchFile:
                raise AirflowException("No files matching")
