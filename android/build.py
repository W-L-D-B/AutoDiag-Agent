"""Build the native AutoDiag Android client with the installed Android SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "android"
WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Toolchain:
    sdk: Path
    java: Path
    tools: Path
    platform: Path

    def java_tool(self, name: str) -> Path:
        return self.java / "bin" / (name + (".exe" if WINDOWS else ""))

    def android_tool(self, name: str) -> Path:
        suffix = (
            ".bat"
            if WINDOWS and name in {"d8", "apksigner"}
            else ".exe"
            if WINDOWS
            else ""
        )
        return self.tools / (name + suffix)


def first_directory(candidates: list[Path | None], description: str) -> Path:
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate.resolve()
    raise ValueError(f"Cannot find {description}; pass its explicit command-line path.")


def environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def discover(
    sdk_root: Path | None, java_home: Path | None, api: int, build_tools: str
) -> Toolchain:
    if sdk_root is not None and not sdk_root.is_dir():
        raise ValueError("Explicit Android SDK directory does not exist")
    if java_home is not None and not java_home.is_dir():
        raise ValueError("Explicit Java home directory does not exist")
    sdk = first_directory(
        [
            sdk_root,
            environment_path("ANDROID_HOME"),
            environment_path("ANDROID_SDK_ROOT"),
            Path(r"D:\Android\Sdk") if WINDOWS else None,
            Path.home() / "Android/Sdk",
            Path("/usr/local/lib/android/sdk"),
        ],
        "Android SDK",
    )
    javac = shutil.which("javac")
    java = first_directory(
        [
            java_home,
            environment_path("AUTODIAG_JAVA_HOME"),
            environment_path("JAVA_HOME"),
            environment_path("JAVA_HOME_17_X64"),
            environment_path("JAVA_HOME_21_X64"),
            Path(r"D:\soft\AndroidStudio\jbr") if WINDOWS else None,
            Path(javac).resolve().parent.parent if javac else None,
        ],
        "JDK (17 or newer)",
    )
    chain = Toolchain(
        sdk,
        java,
        sdk / "build-tools" / build_tools,
        sdk / "platforms" / f"android-{api}",
    )
    required = [chain.platform / "android.jar", chain.platform / "framework.aidl"]
    required += [chain.java_tool(name) for name in ("java", "javac", "jar", "keytool")]
    required += [
        chain.android_tool(name)
        for name in ("aidl", "aapt2", "d8", "zipalign", "apksigner")
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("Missing build dependencies:\n" + "\n".join(missing))
    return chain


def run(
    command: list[str | Path], environment: dict[str, str], log: Path | None = None
) -> str:
    result = subprocess.run(
        [str(item) for item in command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )

    def decode(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gb18030" if WINDOWS else "utf-8", errors="replace")

    output = decode(result.stdout) + decode(result.stderr)
    if log is not None:
        log.write_text(output, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(
            f"{Path(command[0]).name} failed ({result.returncode}):\n{output}"
        )
    return output


def run_transport_tests(
    chain: Toolchain, work: Path, environment: dict[str, str], artifacts: Path
) -> None:
    classes = work / "transport-tests"
    classes.mkdir()
    run(
        [
            chain.java_tool("javac"),
            "-encoding",
            "UTF-8",
            "-source",
            "8",
            "-target",
            "8",
            "-Xlint:-options",
            "-d",
            classes,
            APP / "src/io/autodiag/agent/FramedTransport.java",
            ROOT / "tests/android/FramedTransportTest.java",
        ],
        environment,
    )
    result = run(
        [
            chain.java_tool("java"),
            "-ea",
            "-cp",
            classes,
            "io.autodiag.agent.FramedTransportTest",
        ],
        environment,
        artifacts / "transport-tests.log",
    )
    print(result.strip())


def build(chain: Toolchain, output: Path, tests_only: bool) -> dict:
    if output.suffix.lower() != ".apk" or output.is_symlink() or output.is_dir():
        raise ValueError("Output must name a regular .apk artifact")
    artifacts = output.parent.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    work_root = artifacts / "work"
    work_root.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="build-", dir=work_root))
    environment = dict(os.environ)
    environment["JAVA_HOME"] = str(chain.java)
    environment["ANDROID_HOME"] = str(chain.sdk)
    version = run([chain.java_tool("javac"), "-version"], environment)
    major = re.search(r"javac\s+(\d+)", version)
    if not major or int(major.group(1)) < 17:
        raise ValueError("Android build tools require JDK 17 or newer")
    run_transport_tests(chain, work, environment, artifacts)
    if tests_only:
        return {"transport_tests": "PASS", "work": str(work)}

    generated = work / "generated"
    generated.mkdir()
    compiled = work / "resources.zip"
    run(
        [chain.android_tool("aapt2"), "compile", "--dir", APP / "res", "-o", compiled],
        environment,
    )
    resource_apk = work / "resources.apk"
    run(
        [
            chain.android_tool("aapt2"),
            "link",
            "-o",
            resource_apk,
            "-I",
            chain.platform / "android.jar",
            "--manifest",
            APP / "AndroidManifest.xml",
            "-R",
            compiled,
            "--auto-add-overlay",
            "--java",
            generated,
        ],
        environment,
    )
    for source in sorted((APP / "aidl").rglob("*.aidl")):
        run(
            [
                chain.android_tool("aidl"),
                "--lang=java",
                "--min_sdk_version=26",
                "-I",
                APP / "aidl",
                "-p",
                chain.platform / "framework.aidl",
                "-o",
                generated,
                source,
            ],
            environment,
        )
    classes = work / "classes"
    classes.mkdir()
    sources = sorted((APP / "src").rglob("*.java")) + sorted(generated.rglob("*.java"))
    run(
        [
            chain.java_tool("javac"),
            "-encoding",
            "UTF-8",
            "-source",
            "8",
            "-target",
            "8",
            "-Xlint:-options",
            "-classpath",
            chain.platform / "android.jar",
            "-d",
            classes,
            *sources,
        ],
        environment,
        artifacts / "javac.log",
    )
    class_jar = work / "classes.jar"
    run([chain.java_tool("jar"), "cf", class_jar, "-C", classes, "."], environment)
    dex = work / "dex"
    dex.mkdir()
    run(
        [
            chain.android_tool("d8"),
            "--lib",
            chain.platform / "android.jar",
            "--min-api",
            "26",
            "--output",
            dex,
            class_jar,
        ],
        environment,
        artifacts / "d8.log",
    )
    unsigned = work / "unsigned.apk"
    shutil.copyfile(resource_apk, unsigned)
    with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(dex.glob("classes*.dex")):
            archive.write(entry, entry.name)
    aligned = work / "aligned.apk"
    run([chain.android_tool("zipalign"), "-f", "4", unsigned, aligned], environment)

    cache = Path(
        os.environ.get(
            "AUTODIAG_ANDROID_CACHE", str(Path.home() / ".cache/autodiag/android")
        )
    )
    cache.mkdir(parents=True, exist_ok=True)
    keystore = cache / "debug.keystore"
    if not keystore.exists():
        run(
            [
                chain.java_tool("keytool"),
                "-genkeypair",
                "-alias",
                "androiddebugkey",
                "-keystore",
                keystore,
                "-storepass",
                "android",
                "-keypass",
                "android",
                "-dname",
                "CN=AutoDiag Debug,O=AutoDiag,C=CN",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-validity",
                "3650",
            ],
            environment,
        )
    signed = work / "signed.apk"
    run(
        [
            chain.android_tool("apksigner"),
            "sign",
            "--ks",
            keystore,
            "--ks-pass",
            "pass:android",
            "--key-pass",
            "pass:android",
            "--out",
            signed,
            aligned,
        ],
        environment,
    )
    verification = run(
        [
            chain.android_tool("apksigner"),
            "verify",
            "--verbose",
            "--print-certs",
            signed,
        ],
        environment,
        artifacts / "apk-verification.txt",
    )
    badging = run(
        [chain.android_tool("aapt2"), "dump", "badging", signed],
        environment,
        artifacts / "apk-badging.txt",
    )
    run(
        [
            chain.android_tool("aapt2"),
            "dump",
            "xmltree",
            "--file",
            "AndroidManifest.xml",
            signed,
        ],
        environment,
        artifacts / "manifest-tree.txt",
    )
    if "package: name='io.autodiag.agent'" not in badging:
        raise RuntimeError("Built APK package identity does not match AutoDiag")
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Temporary APK publication path is occupied")
    shutil.copyfile(signed, temporary)
    temporary.replace(output)
    report = {
        "package": "io.autodiag.agent",
        "api": 35,
        "min_sdk": 26,
        "artifact": str(output.resolve()),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "transport_tests": "PASS",
        "signature_verification": "PASS",
        "signed_schemes": [
            line.strip()
            for line in verification.splitlines()
            if line.startswith("Verified using")
        ],
        "work": str(work),
        "sdk": str(chain.sdk),
        "java": str(chain.java),
    }
    (artifacts / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--api", type=int, default=35)
    parser.add_argument("--build-tools", default="35.0.0")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/android/AutoDiag-Agent-debug.apk",
    )
    parser.add_argument("--unit-tests-only", action="store_true")
    args = parser.parse_args()
    if args.api != 35:
        parser.error("This client is locked to compile SDK 35")
    try:
        chain = discover(args.sdk_root, args.java_home, args.api, args.build_tools)
        report = build(chain, args.output.expanduser().absolute(), args.unit_tests_only)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Android build failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
