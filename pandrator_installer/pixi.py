"""Pixi environment and Python requirement management."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

import requests

try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .constants import (
    INSTALLER_STATE_FILENAME,
    NEMO_TEXT_PROCESSING_CDIFFLIB_SHIM,
    NEMO_TEXT_PROCESSING_PIP_DEPS,
    NEMO_TEXT_PROCESSING_SPEC,
    OPTIONAL_REQUIREMENT_EXCLUSIONS_BY_ENV,
    PANDRATOR_RUNTIME_REPAIR_SPECS,
    PIXI_CACHE_DIRNAME,
    PIXI_HOME_DIRNAME,
    PIXI_PIP_CACHE_SUBDIRNAME,
    PIXI_TEMP_SUBDIRNAME,
    SUBDUB_EDITABLE_INSTALL_SPEC,
    SUBDUB_RUNTIME_CHECK_COMMAND,
    SUBDUB_RUNTIME_REPAIR_SPECS,
)
from .platforms import (
    is_windows,
    pixi_binary_name,
    pixi_download_url,
    pixi_manifest_platform,
    pixi_temp_suffix,
)


class PixiEnvironmentMixin:
    def get_pixi_executable(self, pandrator_path):
        return os.path.join(pandrator_path, 'bin', pixi_binary_name())

    def get_pixi_env_dir(self, pandrator_path, env_name):
        return os.path.join(pandrator_path, 'envs', env_name)

    def get_pixi_manifest_path(self, pandrator_path, env_name):
        return os.path.join(self.get_pixi_env_dir(pandrator_path, env_name), 'pixi.toml')

    def get_pixi_subprocess_env(self, pandrator_path):
        pixi_home = os.path.join(pandrator_path, PIXI_HOME_DIRNAME)
        pixi_cache = os.path.join(pandrator_path, PIXI_CACHE_DIRNAME)
        rattler_cache = os.path.join(pixi_cache, 'rattler')
        pip_cache = os.path.join(pixi_cache, PIXI_PIP_CACHE_SUBDIRNAME)
        local_temp = os.path.join(pixi_cache, PIXI_TEMP_SUBDIRNAME)

        os.makedirs(pixi_home, exist_ok=True)
        os.makedirs(pixi_cache, exist_ok=True)
        os.makedirs(rattler_cache, exist_ok=True)
        os.makedirs(pip_cache, exist_ok=True)
        os.makedirs(local_temp, exist_ok=True)

        env = os.environ.copy()
        env['PIXI_HOME'] = pixi_home
        env['PIXI_CACHE_DIR'] = pixi_cache
        env['RATTLER_CACHE_DIR'] = rattler_cache
        env['PIP_CACHE_DIR'] = pip_cache
        env['UV_CACHE_DIR'] = os.path.join(pixi_cache, 'uv-cache')
        env['TMP'] = local_temp
        env['TEMP'] = local_temp
        env['TMPDIR'] = local_temp
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'

        # Unified Portable Model Caches
        local_cache_root = os.path.join(pandrator_path, 'cache')
        env['XDG_CACHE_HOME'] = local_cache_root
        env['HF_HOME'] = os.path.join(local_cache_root, 'huggingface')
        env['HF_HUB_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'hub')
        env['HUGGINGFACE_HUB_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'hub')
        env['TRANSFORMERS_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'transformers')
        env['TORCH_HOME'] = os.path.join(local_cache_root, 'torch')
        env['TTS_HOME'] = os.path.join(local_cache_root, 'tts')
        env['PADDLE_PDX_CACHE_HOME'] = os.path.join(local_cache_root, 'paddlex')
        env['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'

        bundled_ffmpeg_path = self.get_bundled_ffmpeg_executable(pandrator_path)
        if os.path.exists(bundled_ffmpeg_path):
            env['PANDRATOR_FFMPEG_EXE'] = bundled_ffmpeg_path

        return env

    def cleanup_installer_package_caches(self, pandrator_path):
        """Best-effort cleanup of disposable package caches after successful install/update."""
        pixi_cache = os.path.join(pandrator_path, PIXI_CACHE_DIRNAME)
        cleanup_paths = [
            os.path.join(pixi_cache, PIXI_PIP_CACHE_SUBDIRNAME),
            os.path.join(pixi_cache, 'pkgs'),
            os.path.join(pixi_cache, 'rattler'),
            os.path.join(pixi_cache, 'repodata'),
            os.path.join(pixi_cache, 'uv-cache'),
            os.path.join(pixi_cache, PIXI_TEMP_SUBDIRNAME),
        ]

        for cache_path in cleanup_paths:
            if not os.path.exists(cache_path):
                continue
            try:
                if os.path.isdir(cache_path):
                    shutil.rmtree(cache_path)
                else:
                    os.remove(cache_path)
                logging.info("Removed installer package cache: %s", cache_path)
            except OSError as exc:
                logging.warning("Could not remove installer package cache %s: %s", cache_path, exc)

        # Recreate the expected empty cache roots so later launcher operations can reuse them.
        for cache_path in cleanup_paths:
            os.makedirs(cache_path, exist_ok=True)
        self.get_pixi_subprocess_env(pandrator_path)

    def run_pixi_command(self, pandrator_path, arguments, cwd=None, log_errors=True):
        pixi_executable = self.get_pixi_executable(pandrator_path)
        if not os.path.exists(pixi_executable):
            raise FileNotFoundError(
                f"Pixi executable not found at {pixi_executable}. Run Install or Update to set up Pixi."
            )

        return self.run_command(
            [pixi_executable] + arguments,
            cwd=cwd,
            env=self.get_pixi_subprocess_env(pandrator_path),
            log_errors=log_errors,
        )

    def build_pixi_run_command(self, pandrator_path, env_name, command):
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(
                f"Pixi environment manifest not found for {env_name}: {manifest_path}. "
                "Run Install or Update to migrate this installation."
            )

        return [
            self.get_pixi_executable(pandrator_path),
            'run',
            '--manifest-path', manifest_path,
            '--executable',
        ] + command

    def run_pixi_in_env(self, pandrator_path, env_name, command, cwd=None, log_errors=True):
        return self.run_command(
            self.build_pixi_run_command(pandrator_path, env_name, command),
            cwd=cwd,
            env=self.get_pixi_subprocess_env(pandrator_path),
            log_errors=log_errors,
        )

    def check_pixi(self, pandrator_path):
        return os.path.exists(self.get_pixi_executable(pandrator_path))

    def install_pixi(self, pandrator_path):
        logging.info("Installing Pixi...")
        self.configure_tls_certificates()
        bin_path = os.path.join(pandrator_path, 'bin')
        os.makedirs(bin_path, exist_ok=True)

        pixi_executable = self.get_pixi_executable(pandrator_path)
        if os.path.exists(pixi_executable):
            logging.info("Pixi is already installed.")
            return

        temp_fd, temp_pixi_path = tempfile.mkstemp(
            prefix='pandrator_pixi_',
            suffix=pixi_temp_suffix(),
            dir=self.get_local_temp_dir(pandrator_path),
        )
        os.close(temp_fd)
        logging.info("Using temporary Pixi download path: %s", temp_pixi_path)

        try:
            response = requests.get(
                pixi_download_url(),
                stream=True,
                timeout=60,
                verify=self.ca_bundle_path if self.ca_bundle_path else True,
            )
            response.raise_for_status()

            with open(temp_pixi_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            shutil.move(temp_pixi_path, pixi_executable)
            if not is_windows():
                os.chmod(pixi_executable, 0o755)
            self.run_pixi_command(pandrator_path, ['--version'])
            logging.info("Pixi installed successfully.")
        finally:
            if os.path.exists(temp_pixi_path):
                os.remove(temp_pixi_path)

    def update_manifest_python_dependency(self, manifest_path, python_version):
        try:
            with open(manifest_path, 'r', encoding='utf-8', errors='replace') as f:
                manifest_contents = f.read()
        except OSError as e:
            logging.warning(f"Could not read manifest for Python migration ({manifest_path}): {str(e)}")
            return False

        updated_contents = self.update_manifest_contents(manifest_contents, python_version)
        if updated_contents == manifest_contents:
            return False

        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(updated_contents)

        logging.info(
            "Updated Pixi manifest %s to Python %s.* and platform %s.",
            manifest_path,
            python_version,
            pixi_manifest_platform(),
        )
        return True

    def update_manifest_contents(self, manifest_contents, python_version):
        desired_python_line = f'python = "{python_version}.*"'
        desired_platform_line = f'platforms = ["{pixi_manifest_platform()}"]'

        python_line_pattern = r'(?mi)^[ \t]*python[ \t]*=[ \t]*"[^"]*"[ \t]*$'
        if re.search(python_line_pattern, manifest_contents):
            updated_contents = re.sub(python_line_pattern, desired_python_line, manifest_contents, count=1)
        elif '[dependencies]' in manifest_contents:
            updated_contents = manifest_contents.replace('[dependencies]', f'[dependencies]\n{desired_python_line}', 1)
        else:
            newline = '' if manifest_contents.endswith('\n') else '\n'
            updated_contents = f"{manifest_contents}{newline}\n[dependencies]\n{desired_python_line}\n"

        platform_line_pattern = r'(?mi)^[ \t]*platforms[ \t]*=[ \t]*\[[^\]]*\][ \t]*$'
        if re.search(platform_line_pattern, updated_contents):
            updated_contents = re.sub(
                platform_line_pattern,
                desired_platform_line,
                updated_contents,
                count=1,
            )
        elif re.search(r'(?mi)^[ \t]*channels[ \t]*=', updated_contents):
            updated_contents = re.sub(
                r'(?mi)^([ \t]*channels[ \t]*=[^\n]*\n)',
                rf'\1{desired_platform_line}\n',
                updated_contents,
                count=1,
            )
        elif '[workspace]' in updated_contents:
            updated_contents = updated_contents.replace(
                '[workspace]\n',
                f'[workspace]\n{desired_platform_line}\n',
                1,
            )

        return updated_contents

    def ensure_pixi_manifest(self, pandrator_path, env_name, python_version):
        env_dir = self.get_pixi_env_dir(pandrator_path, env_name)
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)

        os.makedirs(env_dir, exist_ok=True)

        if not os.path.exists(manifest_path):
            manifest_contents = (
                "[workspace]\n"
                f"name = \"{env_name}\"\n"
                "channels = [\"conda-forge\"]\n"
                f"platforms = [\"{pixi_manifest_platform()}\"]\n\n"
                "[dependencies]\n"
                f"python = \"{python_version}.*\"\n"
                "pip = \"*\"\n"
            )

            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write(manifest_contents)
        else:
            self.update_manifest_python_dependency(manifest_path, python_version)

        return manifest_path

    def get_env_python_version(self, pandrator_path, env_name):
        stdout, _ = self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
            log_errors=False,
        )

        python_version = stdout.strip().splitlines()[-1] if stdout.strip() else ''
        if not python_version:
            raise RuntimeError(f"Could not detect Python version in {env_name}")

        return python_version

    def create_pixi_env(self, pandrator_path, env_name, python_version):
        logging.info(f"Creating pixi environment {env_name}...")
        manifest_path = self.ensure_pixi_manifest(pandrator_path, env_name, python_version)

        try:
            self.run_pixi_command(
                pandrator_path,
                ['install', '--manifest-path', manifest_path],
                cwd=self.get_pixi_env_dir(pandrator_path, env_name)
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to create or set up pixi environment {env_name}")
            logging.error(f"Error output: {e.stderr}")
            raise

    def add_pixi_conda_package(self, pandrator_path, env_name, package_spec):
        logging.info(f"Adding {package_spec} to {env_name} via pixi...")
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        package_name, separator, package_version = package_spec.partition('=')
        package_name = package_name.strip()
        package_version = package_version.strip() if separator else None

        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if line.strip().startswith(f'{package_name} ='):
                        if package_version is None or f'"{package_version}"' in line:
                            logging.info(f"{package_name} is already present in {env_name}, skipping pixi add.")
                            return
                        logging.info(f"{package_name} is present with a different version in {env_name}, updating it.")
                        break

        self.run_pixi_command(
            pandrator_path,
            ['add', '--manifest-path', manifest_path, package_spec],
            cwd=self.get_pixi_env_dir(pandrator_path, env_name)
        )

    def get_installer_state_path(self, pandrator_path):
        return os.path.join(pandrator_path, INSTALLER_STATE_FILENAME)

    def load_installer_state(self, pandrator_path):
        state_path = self.get_installer_state_path(pandrator_path)
        default_state = {'requirements_hashes': {}}

        if not os.path.exists(state_path):
            return default_state

        try:
            with open(state_path, 'r', encoding='utf-8', errors='replace') as f:
                state = json.load(f)

            if not isinstance(state, dict):
                raise ValueError("installer state root must be a dictionary")
        except Exception as e:
            logging.warning(f"Failed to load installer state from {state_path}: {str(e)}")
            return default_state

        requirements_hashes = state.get('requirements_hashes')
        if not isinstance(requirements_hashes, dict):
            state['requirements_hashes'] = {}

        return state

    def save_installer_state(self, pandrator_path, state):
        state_path = self.get_installer_state_path(pandrator_path)
        os.makedirs(pandrator_path, exist_ok=True)

        serializable_state = state if isinstance(state, dict) else {'requirements_hashes': {}}
        if not isinstance(serializable_state.get('requirements_hashes'), dict):
            serializable_state['requirements_hashes'] = {}

        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(serializable_state, f, indent=2, sort_keys=True)
        except Exception as e:
            logging.warning(f"Failed to save installer state to {state_path}: {str(e)}")

    def calculate_file_sha256(self, file_path):
        digest = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def build_requirements_state_key(self, pandrator_path, env_name, requirements_file):
        try:
            relative_path = os.path.relpath(requirements_file, pandrator_path)
        except ValueError:
            relative_path = os.path.abspath(requirements_file)

        normalized_relative_path = relative_path.replace('\\', '/')
        return f"{env_name}:{normalized_relative_path}"

    def record_requirements_hash(self, pandrator_path, env_name, requirements_file):
        if not os.path.exists(requirements_file):
            return

        state = self.load_installer_state(pandrator_path)
        requirements_hashes = state.setdefault('requirements_hashes', {})
        state_key = self.build_requirements_state_key(pandrator_path, env_name, requirements_file)
        requirements_hashes[state_key] = self.calculate_file_sha256(requirements_file)
        self.save_installer_state(pandrator_path, state)

    def normalize_package_name(self, package_name):
        return package_name.strip().lower().replace('_', '-').replace('.', '-')

    def parse_package_spec(self, package_spec):
        spec = package_spec.split(';', 1)[0].strip()
        if not spec:
            return '', None, ''

        if '@' in spec:
            package_before_at = spec.split('@', 1)[0].strip()
            if package_before_at:
                spec = package_before_at

        package_name = spec
        comparator = None
        version = ''

        for candidate_comparator in ('===', '==', '>=', '<=', '~=', '!=', '>', '<', '='):
            if candidate_comparator in spec:
                package_name, version = spec.split(candidate_comparator, 1)
                package_name = package_name.strip()
                version = version.strip()
                comparator = candidate_comparator
                break

        package_name = package_name.split('[', 1)[0].strip()
        return package_name, comparator, version

    def get_optional_requirement_exclusions(self, env_name):
        configured_exclusions = OPTIONAL_REQUIREMENT_EXCLUSIONS_BY_ENV.get(env_name, ())
        return {
            self.normalize_package_name(package_name)
            for package_name in configured_exclusions
            if package_name
        }

    def should_skip_requirement_line(self, line, excluded_packages):
        if not excluded_packages:
            return False

        package_name, _, _ = self.parse_package_spec(line)
        normalized_package_name = self.normalize_package_name(package_name) if package_name else ''
        if normalized_package_name in excluded_packages:
            return True

        lower_line = line.lower()
        if not any(marker in lower_line for marker in ('git+', 'http://', 'https://', 'file://')):
            return False

        normalized_line = lower_line.replace('_', '-')
        for excluded_package in excluded_packages:
            if re.search(rf'(^|[^a-z0-9]){re.escape(excluded_package)}([^a-z0-9]|$)', normalized_line):
                return True

        return False

    def filter_requirements_text(self, requirements_text, env_name):
        excluded_packages = self.get_optional_requirement_exclusions(env_name)
        if not excluded_packages:
            return requirements_text, []

        filtered_lines = []
        skipped_lines = []

        for raw_line in requirements_text.splitlines():
            line = raw_line.split('#', 1)[0].strip()
            if line and self.should_skip_requirement_line(line, excluded_packages):
                skipped_lines.append(line)
                continue

            filtered_lines.append(raw_line)

        filtered_requirements_text = '\n'.join(filtered_lines)
        if requirements_text.endswith('\n'):
            filtered_requirements_text += '\n'

        return filtered_requirements_text, skipped_lines

    def get_installed_pip_packages(self, pandrator_path, env_name):
        try:
            stdout, _ = self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'freeze'],
                log_errors=False,
            )
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Failed to inspect pip packages in {env_name}; package checks will require reinstall. STDERR: {e.stderr}"
            )
            return None

        installed_packages = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or line.startswith('-e '):
                continue

            if ' @ ' in line:
                package_name = line.split(' @ ', 1)[0].strip()
                if package_name:
                    installed_packages[self.normalize_package_name(package_name)] = None
                continue

            if '==' not in line:
                continue

            package_name, version = line.split('==', 1)
            installed_packages[self.normalize_package_name(package_name)] = version.strip()

        return installed_packages

    def get_installed_pip_freeze_entries(self, pandrator_path, env_name):
        try:
            stdout, _ = self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'freeze'],
                log_errors=False,
            )
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Failed to inspect pip freeze entries in {env_name}; source checks will require reinstall. STDERR: {e.stderr}"
            )
            return None

        freeze_entries = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or line.startswith('-e '):
                continue

            package_name = ''
            if ' @ ' in line:
                package_name = line.split(' @ ', 1)[0].strip()
            elif '==' in line:
                package_name = line.split('==', 1)[0].strip()

            if package_name:
                freeze_entries[self.normalize_package_name(package_name)] = line

        return freeze_entries

    def find_unsatisfied_package_specs(self, package_specs, installed_packages):
        if installed_packages is None:
            return list(package_specs)

        unsatisfied_specs = []
        for package_spec in package_specs:
            package_name, comparator, expected_version = self.parse_package_spec(package_spec)
            if not package_name:
                continue

            normalized_package_name = self.normalize_package_name(package_name)
            if normalized_package_name not in installed_packages:
                unsatisfied_specs.append(package_spec)
                continue

            installed_version = installed_packages.get(normalized_package_name)
            spec_satisfied = self.requirement_spec_satisfied(
                installed_version,
                comparator,
                expected_version,
            )
            if spec_satisfied is False:
                unsatisfied_specs.append(package_spec)

        return unsatisfied_specs

    def requirement_spec_satisfied(self, installed_version, comparator, expected_version):
        if comparator is None or not expected_version:
            return True

        if not installed_version:
            return False

        if comparator in ('==', '===', '=') and self.versions_match_exact_spec(installed_version, expected_version):
            return True

        if PackagingSpecifierSet is None:
            if comparator in ('==', '===', '='):
                return False
            return None

        normalized_comparator = '==' if comparator in ('===', '=') else comparator
        specifier_text = f"{normalized_comparator}{expected_version}"

        try:
            return PackagingSpecifierSet(specifier_text).contains(installed_version, prereleases=True)
        except Exception:
            logging.debug(
                "Could not evaluate version specifier '%s' against installed version '%s'",
                specifier_text,
                installed_version,
            )

            if comparator in ('==', '===', '='):
                return False

            return None

    def versions_match_exact_spec(self, installed_version, expected_version):
        if installed_version == expected_version:
            return True

        if not installed_version or not expected_version:
            return False

        if '+' not in expected_version and '+' in installed_version:
            return installed_version.split('+', 1)[0] == expected_version

        return False

    def format_package_specs(self, package_specs, max_items=5):
        if not package_specs:
            return ''

        preview = ', '.join(package_specs[:max_items])
        if len(package_specs) > max_items:
            preview += ', ...'
        return preview

    def should_install_requirements(self, pandrator_path, env_name, requirements_file):
        if not os.path.exists(requirements_file):
            raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            return True, "Pixi manifest is missing"

        state = self.load_installer_state(pandrator_path)
        requirements_hashes = state.setdefault('requirements_hashes', {})
        state_key = self.build_requirements_state_key(pandrator_path, env_name, requirements_file)
        current_hash = self.calculate_file_sha256(requirements_file)
        previous_hash = requirements_hashes.get(state_key)

        _, _, requirement_specs, unsupported_lines, _ = self.load_pypi_requirements(requirements_file, env_name)
        installed_packages = self.get_installed_pip_packages(pandrator_path, env_name)
        unsatisfied_specs = self.find_unsatisfied_package_specs(requirement_specs, installed_packages)

        if unsatisfied_specs:
            return True, (
                "missing or mismatched packages "
                f"({self.format_package_specs(unsatisfied_specs)})"
            )

        if previous_hash == current_hash:
            return False, "requirements unchanged and package checks passed"

        if unsupported_lines:
            return True, "requirements changed and include entries that require pip -r"

        requirements_hashes[state_key] = current_hash
        self.save_installer_state(pandrator_path, state)
        return False, "requirements changed but package checks passed"

    def component_needs_package_sync(self, pandrator_path, env_name, package_specs):
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            return True, "Pixi manifest is missing"

        installed_packages = self.get_installed_pip_packages(pandrator_path, env_name)
        unsatisfied_specs = self.find_unsatisfied_package_specs(package_specs, installed_packages)
        if unsatisfied_specs:
            return True, (
                "missing or mismatched packages "
                f"({self.format_package_specs(unsatisfied_specs)})"
            )

        return False, "package checks passed"

    def package_source_matches(self, pandrator_path, env_name, package_name, expected_source_fragment):
        freeze_entries = self.get_installed_pip_freeze_entries(pandrator_path, env_name)
        if freeze_entries is None:
            return False, "pip freeze inspection failed"

        normalized_package_name = self.normalize_package_name(package_name)
        freeze_entry = freeze_entries.get(normalized_package_name)
        if not freeze_entry:
            return False, f"{package_name} is not installed"

        if ' @ ' not in freeze_entry:
            return False, f"{package_name} is not installed from an explicit source"

        if expected_source_fragment.lower() not in freeze_entry.lower():
            return False, f"{package_name} is installed from a different source"

        return True, "source check passed"

    def extract_import_candidates(self, requirements_file, env_name=None):
        candidates = []
        seen = set()
        import_aliases = {
            'google-genai': 'google.genai',
            'pymupdf': 'fitz',
            'ffmpeg-python': 'ffmpeg',
            'beautifulsoup4': 'bs4',
            'pillow': 'PIL',
        }

        with open(requirements_file, 'rb') as f:
            requirements_text = f.read().decode('utf-8-sig', errors='replace')

        filtered_requirements_text, _ = self.filter_requirements_text(requirements_text, env_name)

        for raw_line in filtered_requirements_text.splitlines():
            line = raw_line.split('#', 1)[0].strip()
            if not line or line.startswith(('-', 'git+', 'http://', 'https://', '.', '/')):
                continue

            requirement = line.split(';', 1)[0].strip()
            requirement = requirement.split('@', 1)[0].strip()

            package_name = requirement
            for separator in ('[', '==', '>=', '<=', '~=', '!=', '>', '<', '='):
                package_name = package_name.split(separator, 1)[0].strip()

            normalized_package_name = package_name.lower().replace('_', '-').replace('.', '-')
            import_name = import_aliases.get(normalized_package_name, package_name.replace('-', '_'))
            if import_name and import_name not in seen:
                seen.add(import_name)
                candidates.append(import_name)

        return candidates

    def load_pypi_requirements(self, requirements_file, env_name=None):
        requirement_specs = []
        unsupported_lines = []

        with open(requirements_file, 'rb') as f:
            requirements_text = f.read().decode('utf-8-sig', errors='replace')

        filtered_requirements_text, skipped_lines = self.filter_requirements_text(requirements_text, env_name)

        for raw_line in filtered_requirements_text.splitlines():
            line = raw_line.split('#', 1)[0].strip()
            if not line:
                continue

            lower_line = line.lower()
            has_direct_reference = ' @ ' in line and any(
                marker in lower_line for marker in ('git+', 'http://', 'https://', 'file://')
            )

            if line.startswith(('-', 'git+', 'http://', 'https://', 'file://', '.', '/')) or has_direct_reference:
                unsupported_lines.append(line)
                continue

            requirement_specs.append(line)

        return requirements_text, filtered_requirements_text, requirement_specs, unsupported_lines, skipped_lines

    def add_pypi_requirements(self, pandrator_path, env_name, requirement_specs):
        if not requirement_specs:
            return []

        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        env_dir = self.get_pixi_env_dir(pandrator_path, env_name)

        try:
            self.run_pixi_command(
                pandrator_path,
                ['add', '--manifest-path', manifest_path, '--pypi'] + requirement_specs,
                cwd=env_dir,
                log_errors=False,
            )
            return []
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Bulk pixi add failed for {env_name}; retrying requirements one-by-one. STDERR: {e.stderr}"
            )

        failed_specs = []
        for requirement_spec in requirement_specs:
            try:
                self.run_pixi_command(
                    pandrator_path,
                    ['add', '--manifest-path', manifest_path, '--pypi', requirement_spec],
                    cwd=env_dir,
                    log_errors=False,
                )
            except subprocess.CalledProcessError as e:
                failed_specs.append(requirement_spec)
                logging.warning(
                    f"pixi add failed for '{requirement_spec}' in {env_name}. "
                    f"Will try pip fallback for this requirement. STDERR: {e.stderr}"
                )

        return failed_specs

    def install_requirement_specs_with_pip(self, pandrator_path, env_name, requirement_specs):
        for requirement_spec in requirement_specs:
            logging.info(f"Installing requirement via pip fallback in {env_name}: {requirement_spec}")
            if requirement_spec == NEMO_TEXT_PROCESSING_SPEC:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', '--no-deps', requirement_spec]
                )
                self._install_nemo_text_processing_pip_deps(pandrator_path, env_name)
            else:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', requirement_spec]
                )

    def _install_nemo_text_processing_pip_deps(self, pandrator_path, env_name):
        cdifflib_failed = False
        for dep in NEMO_TEXT_PROCESSING_PIP_DEPS:
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', dep],
                    log_errors=False,
                )
            except subprocess.CalledProcessError:
                if dep == 'cdifflib':
                    cdifflib_failed = True
                    logging.warning(
                        "cdifflib build failed (requires MSVC Build Tools). "
                        "Installing difflib fallback shim for nemo_text_processing."
                    )
                else:
                    raise
        if cdifflib_failed:
            self._install_cdifflib_shim(pandrator_path, env_name)

    def _install_cdifflib_shim(self, pandrator_path, env_name):
        stdout, _ = self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
            log_errors=False,
        )
        site_packages = stdout.strip()
        shim_path = os.path.join(site_packages, 'cdifflib.py')
        with open(shim_path, 'w', encoding='utf-8') as f:
            f.write(NEMO_TEXT_PROCESSING_CDIFFLIB_SHIM)
        logging.info("cdifflib fallback shim written to %s", shim_path)

    def ensure_pandrator_runtime(self, pandrator_path, env_name):
        if env_name != 'pandrator_installer':
            return

        check_command = ['python', '-c', 'from PyQt6.QtWidgets import QApplication; import PyQt6.sip; import pygame']
        logging.info("Checking Pandrator runtime imports (PyQt6 + pygame) in pandrator_installer...")

        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                check_command,
                log_errors=False,
            )
            logging.info("Pandrator runtime import check passed.")
            return
        except subprocess.CalledProcessError as e:
            logging.warning(
                "Pandrator runtime import check failed in pandrator_installer. "
                f"Reinstalling runtime packages {PANDRATOR_RUNTIME_REPAIR_SPECS}. STDERR: {e.stderr}"
            )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            [
                'python', '-m', 'pip', 'install',
                '--upgrade', '--force-reinstall', '--no-cache-dir',
                *PANDRATOR_RUNTIME_REPAIR_SPECS,
            ]
        )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            check_command,
            log_errors=False,
        )
        logging.info(
            "Pandrator runtime repaired successfully using %s.",
            ', '.join(PANDRATOR_RUNTIME_REPAIR_SPECS),
        )

    def ensure_pyqt6_runtime(self, pandrator_path, env_name):
        self.ensure_pandrator_runtime(pandrator_path, env_name)

    def ensure_subdub_runtime(self, pandrator_path, env_name, subdub_repo_path):
        if env_name != 'pandrator_installer':
            return

        if not os.path.isdir(subdub_repo_path):
            logging.warning(
                f"Skipping Subdub runtime import check because repository path does not exist: {subdub_repo_path}"
            )
            return

        check_command = SUBDUB_RUNTIME_CHECK_COMMAND
        logging.info(
            "Checking Subdub runtime imports (subdub + litellm + tiktoken + fastuuid + "
            "PyQt6 + matplotlib + sounddevice) in pandrator_installer..."
        )

        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                check_command,
                cwd=subdub_repo_path,
                log_errors=False,
            )
            logging.info("Subdub runtime import check passed.")
            return
        except subprocess.CalledProcessError as e:
            logging.warning(
                "Subdub runtime import check failed in pandrator_installer. "
                f"Reinstalling runtime packages {SUBDUB_RUNTIME_REPAIR_SPECS}. STDERR: {e.stderr}"
            )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            [
                'python',
                '-m',
                'pip',
                'install',
                '--upgrade',
                '--force-reinstall',
                '--no-cache-dir',
                *SUBDUB_RUNTIME_REPAIR_SPECS,
            ],
            cwd=subdub_repo_path,
        )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', '--no-deps', '-e', SUBDUB_EDITABLE_INSTALL_SPEC],
            cwd=subdub_repo_path,
        )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            check_command,
            cwd=subdub_repo_path,
            log_errors=False,
        )

        logging.info(
            "Subdub runtime repaired successfully using %s.",
            ', '.join(SUBDUB_RUNTIME_REPAIR_SPECS),
        )

    def try_import_requirements(self, pandrator_path, env_name, requirements_file):
        logging.info(f"Running best-effort import checks for {requirements_file}...")

        for import_name in self.extract_import_candidates(requirements_file, env_name):
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-c', f'import importlib; importlib.import_module("{import_name}")'],
                    log_errors=False,
                )
            except subprocess.CalledProcessError as e:
                logging.warning(
                    f"Import check failed for {import_name} after installing {requirements_file}. "
                    f"This is best-effort only and may be expected on Windows. STDERR: {e.stderr}"
                )

    def install_requirements(self, pandrator_path, env_name, requirements_file):
        logging.info(f"Installing requirements for {env_name}...")

        (
            requirements_text,
            filtered_requirements_text,
            requirement_specs,
            unsupported_lines,
            skipped_lines,
        ) = self.load_pypi_requirements(requirements_file, env_name)
        logging.info(f"Requirements file contents:\n{requirements_text}")

        if skipped_lines:
            logging.info(f"Skipping optional requirements for {env_name}: {skipped_lines}")

        failed_pixi_specs = []

        if requirement_specs:
            failed_pixi_specs = self.add_pypi_requirements(pandrator_path, env_name, requirement_specs)
        else:
            logging.info(f"No installable requirements found in {requirements_file}")

        if failed_pixi_specs:
            logging.warning(
                f"Falling back to pip install for requirements that pixi could not add in {env_name}: "
                f"{failed_pixi_specs}"
            )
            self.install_requirement_specs_with_pip(pandrator_path, env_name, failed_pixi_specs)

        if unsupported_lines:
            pip_requirements_file = requirements_file
            temporary_requirements_file = None
            if skipped_lines:
                temp_fd, temporary_requirements_file = tempfile.mkstemp(
                    prefix='pandrator_filtered_requirements_',
                    suffix='.txt',
                    dir=self.get_local_temp_dir(pandrator_path),
                )
                os.close(temp_fd)
                with open(temporary_requirements_file, 'w', encoding='utf-8') as f:
                    f.write(filtered_requirements_text)

                pip_requirements_file = temporary_requirements_file

            logging.warning(
                "Unsupported requirement lines for pixi add detected; "
                f"falling back to pip install -r for {pip_requirements_file}: {unsupported_lines}"
            )
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', '-r', pip_requirements_file]
                )
            finally:
                if temporary_requirements_file and os.path.exists(temporary_requirements_file):
                    try:
                        os.remove(temporary_requirements_file)
                    except OSError as e:
                        logging.warning(
                            f"Failed to remove temporary requirements file {temporary_requirements_file}: {e}"
                        )

        self.ensure_pandrator_runtime(pandrator_path, env_name)

        self.try_import_requirements(pandrator_path, env_name, requirements_file)

        if env_name == 'pandrator_installer':
            logging.info("Checking if dulwich is installed in pandrator_installer environment...")
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-c', 'import dulwich; print(f"Dulwich version {dulwich.__version__} is installed")'],
                    log_errors=False,
                )
                logging.info("Dulwich check completed successfully")
            except subprocess.CalledProcessError:
                logging.warning("Dulwich not found in pandrator_installer environment, installing separately...")
                try:
                    self.run_pixi_in_env(
                        pandrator_path,
                        env_name,
                        ['python', '-m', 'pip', 'install', 'dulwich']
                    )
                    logging.info("Dulwich installed successfully in pandrator_installer environment")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install dulwich in pandrator_installer environment: {str(e)}")
                    raise

        self.record_requirements_hash(pandrator_path, env_name, requirements_file)

    def install_package(self, pandrator_path, env_name, package):
        logging.info(f"Installing {package} in {env_name}...")
        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', package]
        )
