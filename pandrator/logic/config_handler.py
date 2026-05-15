import os
import platform
import subprocess

def save_api_key(key_name: str, key_value: str):
    """
    Saves an API key as a persistent environment variable.
    Raises OSError or subprocess.CalledProcessError on failure.
    """
    system = platform.system()

    if system == "Windows":
        # For Windows, use setx to make the variable persistent.
        subprocess.run(['setx', key_name, key_value], check=True)
    elif system in ["Linux", "Darwin"]:  # Darwin is for macOS
        # For Linux and macOS, append to .bashrc.
        # Note: This may not work for other shells like zsh.
        home = os.path.expanduser("~")
        with open(os.path.join(home, ".bashrc"), "a") as bashrc:
            bashrc.write(f'\nexport {key_name}="{key_value}"')
    else:
        raise OSError("Unsupported operating system")

    # Also set the environment variable for the current running session
    os.environ[key_name] = key_value

def get_api_key(key_name: str) -> str:
    """Gets an API key from the environment variables."""
    return os.getenv(key_name, "")
