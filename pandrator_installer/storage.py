"""Configuration, packaging layout, backup, and concurrent task helpers."""

import concurrent.futures
import json
import logging
import os
import shutil
import tempfile
import traceback
from datetime import datetime, timezone


try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .constants import (
    PACKAGING_COMPONENT_PATHS,
    PACKAGING_CONFIG_FLAGS,
    PACKAGING_EXCLUDED_FILE_PREFIXES,
    PACKAGING_LAYOUT_FILENAME,
    PACKAGING_SHARED_PATHS,
    RVC_GPU_SUPPORT_CONFIG_FLAG,
)
from .platforms import is_windows, pixi_env_python_path


class StorageMixin:
    def execute_concurrently(self, tasks, max_workers=8):
        """Execute multiple callables concurrently and log errors if they fail.

        tasks: dict of {task_name: (callable_fn, args, kwargs)} or {task_name: callable_fn}
        """
        task_names = ", ".join(tasks.keys())
        logging.info(f"Starting concurrent tasks: {task_names}")
        self.reporter.status(f"Running concurrent tasks: {task_names}...")

        results = {}
        errors = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_name = {}
            for name, task in tasks.items():
                if isinstance(task, tuple):
                    fn, args, kwargs = task
                else:
                    fn, args, kwargs = task, (), {}

                def wrapped_fn(fn=fn, args=args, kwargs=kwargs, name=name):
                    logging.info(f"[{name}] Started")
                    res = fn(*args, **kwargs)
                    logging.info(f"[{name}] Completed successfully")
                    return res

                future = executor.submit(wrapped_fn)
                future_to_name[future] = name

            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    logging.error(f"[{name}] Failed: {str(e)}")
                    logging.error(traceback.format_exc())
                    errors[name] = e

        if errors:
            first_error_name = list(errors.keys())[0]
            first_error = errors[first_error_name]
            raise RuntimeError(f"Concurrent task '{first_error_name}' failed: {str(first_error)}") from first_error

        return results

    def get_packaging_layout(self):
        return {
            'layout_version': 1,
            'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds') + 'Z',
            'config_flags': list(PACKAGING_CONFIG_FLAGS),
            'shared_paths': list(PACKAGING_SHARED_PATHS),
            'excluded_file_prefixes': list(PACKAGING_EXCLUDED_FILE_PREFIXES),
            'component_paths': {
                component: list(paths)
                for component, paths in PACKAGING_COMPONENT_PATHS.items()
            },
        }

    def write_packaging_layout(self, pandrator_path):
        layout_path = os.path.join(pandrator_path, PACKAGING_LAYOUT_FILENAME)
        layout = self.get_packaging_layout()

        try:
            with open(layout_path, 'w', encoding='utf-8') as f:
                json.dump(layout, f, indent=2, sort_keys=True)
        except Exception as e:
            logging.warning(f"Failed to write packaging layout file {layout_path}: {str(e)}")

    def get_install_config_path(self, pandrator_path):
        return os.path.join(pandrator_path, 'config.json')

    def load_install_config(self, pandrator_path, detect_rvc=False):
        config_path = self.get_install_config_path(pandrator_path)

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8', errors='replace') as f:
                    config = json.load(f)

                if not isinstance(config, dict):
                    raise ValueError("config root must be a dictionary")
            except Exception as e:
                logging.warning(f"Failed to load config from {config_path}: {str(e)}")
                config = {}
        else:
            config = {}

        if detect_rvc:
            config = self.ensure_rvc_support_flag(pandrator_path, config)

        return config

    def save_install_config(self, pandrator_path, config):
        config_path = self.get_install_config_path(pandrator_path)
        os.makedirs(pandrator_path, exist_ok=True)

        serializable_config = config if isinstance(config, dict) else {}
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='.config-',
            suffix='.tmp',
            dir=pandrator_path,
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
                descriptor = -1
                json.dump(serializable_config, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, config_path)
        except Exception as e:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass
            raise RuntimeError(f"Failed to save install config to {config_path}: {str(e)}") from e

    def backup_state_database(self, pandrator_repo_path):
        """Creates a timestamped backup of pandrator_state.sqlite3 before update."""
        try:
            db_prefix = 'pandrator_state.sqlite3'
            candidate_files = []
            for file_name in os.listdir(pandrator_repo_path):
                if file_name.lower().startswith(db_prefix):
                    candidate_files.append(file_name)

            if not candidate_files:
                return []

            backup_root = os.path.join(pandrator_repo_path, 'db_backups')
            os.makedirs(backup_root, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            backup_dir = os.path.join(backup_root, timestamp)
            os.makedirs(backup_dir, exist_ok=True)

            backed_up_paths = []
            for file_name in sorted(candidate_files):
                source_path = os.path.join(pandrator_repo_path, file_name)
                if not os.path.isfile(source_path):
                    continue

                destination_path = os.path.join(backup_dir, file_name)
                try:
                    shutil.copy2(source_path, destination_path)
                    backed_up_paths.append(destination_path)
                except Exception as e:
                    logging.warning(f"Could not back up state database file {source_path}: {str(e)}")

            if not backed_up_paths:
                try:
                    os.rmdir(backup_dir)
                except OSError:
                    pass

            return backed_up_paths
        except Exception as e:
            logging.warning(f"State database backup step failed: {str(e)}")
            return []

    def ensure_rvc_support_flag(self, pandrator_path, config):
        if config.get('rvc_support', False) and RVC_GPU_SUPPORT_CONFIG_FLAG in config:
            return config

        rvc_run_scripts = (
            os.path.join(pandrator_path, 'rvc-python', 'run.bat'),
            os.path.join(pandrator_path, 'rvc-python', 'run.py'),
        )
        rvc_gpu_python = pixi_env_python_path(
            os.path.join(pandrator_path, 'rvc-python', '.pixi', 'envs', 'default'),
            system='windows' if is_windows() else 'linux',
        )
        legacy_site_packages = os.path.join(
            pandrator_path,
            'envs',
            'pandrator_installer',
            '.pixi',
            'envs',
            'default',
            'Lib',
            'site-packages',
        )
        legacy_rvc_detected = False
        if os.path.isdir(legacy_site_packages):
            try:
                for entry in os.listdir(legacy_site_packages):
                    normalized_entry = entry.lower().replace('_', '-').replace('.', '-')
                    if normalized_entry == 'rvc-python' or normalized_entry.startswith('rvc-python-'):
                        legacy_rvc_detected = True
                        break
            except OSError as exc:
                logging.warning("Could not inspect the legacy RVC installation: %s", exc)

        if (
            not config.get('rvc_support', False)
            and not any(os.path.exists(path) for path in rvc_run_scripts)
            and not legacy_rvc_detected
        ):
            return config

        updated_config = dict(config)
        updated_config['rvc_support'] = True
        updated_config.setdefault(RVC_GPU_SUPPORT_CONFIG_FLAG, os.path.exists(rvc_gpu_python))
        self.save_install_config(pandrator_path, updated_config)
        source = "legacy in-process RVC installation" if legacy_rvc_detected else "RVC service repository"
        logging.info("Detected %s and persisted rvc_support=true in config.json", source)
        return updated_config