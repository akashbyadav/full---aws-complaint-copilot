"""Optional maintainer rebuild of the supplied Glue wheel and AgentCore ZIP.

Upload the prepared files in deployment/artifacts/ directly. After editing
application code, you can rebuild them from the project root:
    python -m deployment.package_runtime

To rebuild only the small shared library for Glue:
    python -m deployment.package_runtime --glue-only

The artifact preserves conversational_agent/, shared/, config/ and the small
data/guidance/articles.json file. Its generated root launcher makes Python
imports resolve from that root inside AgentCore.
Dependencies are downloaded for Linux ARM64 rather than copied from a Mac venv.
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_glue_wheel(artifacts):
    """Build an installable pure-Python wheel containing the shared package.

    Glue Python shell documents wheel files for its --extra-py-files library
    setting. A plain source ZIP can have the right folder layout and still not
    be installed by that environment. setuptools adds the wheel metadata that
    Python package installers need; renaming a ZIP to .whl is not sufficient.

    The staging directory contains only shared/ and its packaging configuration.
    The wheel has no bundled native dependencies and supports Python 3.9+.
    Glue installs boto3 and pyarrow through --additional-python-modules instead.
    """
    staging = PROJECT_ROOT / "build" / "glue-library"

    # Recreate only disposable build output, keeping the original shared source
    # files and the already prepared AgentCore artifact intact.
    if staging.exists():
        shutil.rmtree(staging)

    staging.mkdir(parents=True)
    shutil.copytree(
        PROJECT_ROOT / "shared",
        staging / "shared",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(
        PROJECT_ROOT / "deployment" / "glue" / "pyproject.toml",
        staging / "pyproject.toml",
    )

    # pip builds with the pinned setuptools/wheel tools in an isolated temporary
    # environment. No AWS credentials or service calls are involved in building.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(artifacts),
            str(staging),
        ],
        check=True,
    )


def copy_application_files(target, config_path):
    """Copy runtime code, configuration and guidance data into the package.

    Args:
        target: Build directory that will become the root of the deployment ZIP.
        config_path: The supplied configuration to package as config/config.json.

    The UI, input batches and any cached source archive are not needed by the
    running agent. Keeping them out also makes the artifact easier to inspect.
    """
    for package_name in ("conversational_agent", "shared"):
        shutil.copytree(
            PROJECT_ROOT / package_name,
            target / package_name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    packaged_config = target / "config"
    packaged_config.mkdir(parents=True, exist_ok=True)

    shutil.copy2(config_path, packaged_config / "config.json")

    # The existing article data supplies the approved topic IDs and URLs for
    # live guidance. It is about 30 KB; no separate URL catalog is required.
    # Copy only this file, leaving the source archive and replay batches out.
    packaged_guidance = target / "data" / "guidance"
    packaged_guidance.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        PROJECT_ROOT / "data" / "guidance" / "articles.json",
        packaged_guidance / "articles.json",
    )

    # Python puts the launched script's directory on its import path. A launcher
    # at the ZIP root therefore exposes both application packages without manual
    # sys.path edits or a different set of cloud-only import statements.
    launcher = (
        '"""Generated launcher for the folder-based AgentCore application."""\n\n'
        "from conversational_agent.runtime import main\n\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )
    (target / "runtime_entrypoint.py").write_text(launcher)


def create_archive(source_directory, archive_path):
    """ZIP a build directory while preserving package paths and excluding caches.

    Files use paths relative to the build root. Wrapping them in an extra outer
    directory would hide the configured entry point from AgentCore.
    """
    with zipfile.ZipFile(
        archive_path,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(source_directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(source_directory))


def main():
    """Build the Glue wheel and, unless skipped, the AgentCore deployment ZIP.

    build/runtime/ is a temporary staging directory. Final files go into
    deployment/artifacts/. This function never authenticates to AWS or creates
    resources. Account and resource identifiers are resolved by the running app.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--glue-only",
        action="store_true",
        help="Rebuild only the shared wheel; leave the AgentCore ZIP unchanged.",
    )
    arguments = parser.parse_args()

    config_path = PROJECT_ROOT / "config" / "config.json"
    target = PROJECT_ROOT / "build" / "runtime"
    artifacts = PROJECT_ROOT / "deployment" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    build_glue_wheel(artifacts)

    if arguments.glue_only:
        print("Prepared Glue shared-library wheel in:", artifacts)
        return

    # Clear only this generated build directory. Source and downloaded input data
    # live elsewhere and are never removed by the packaging operation.
    if target.exists():
        shutil.rmtree(target)

    target.mkdir(parents=True)

    # Request binary wheels for the actual AWS architecture. Installing our local
    # Mac wheels into the ZIP could fail when importing native dependencies.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--platform",
            "manylinux2014_aarch64",
            "--python-version",
            "3.11",
            "--implementation",
            "cp",
            "--only-binary=:all:",
            "--target",
            str(target),
            "-r",
            str(PROJECT_ROOT / "deployment" / "requirements-runtime.txt"),
        ],
        check=True,
    )

    copy_application_files(target, config_path)

    archive_path = artifacts / "agent.zip"
    create_archive(target, archive_path)

    print("Prepared Glue and AgentCore upload files in:", artifacts)


if __name__ == "__main__":
    main()
