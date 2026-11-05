#!/usr/bin/env python3

__license__ = "MIT"

import argparse
import base64
import binascii
import concurrent.futures
import json
import subprocess
import tempfile

from pathlib import Path
from urllib.request import HTTPError, Request, urlopen

def package_source_get_base_address(src_index: str, cache: dict[str, str]):
    if (cache.get(src_index) is not None):
        return cache[src_index]

    with urlopen(src_index) as resp:
        index = json.load(resp)
        base = next(
            (
                res["@id"]
                for res in index["resources"]
                if any(
                    isinstance(res["@type"], str) and res["@type"].startswith("PackageBaseAddress/")
                    for _ in [0]
                )
            ),
            None,
        )
        cache[src_index] = base
        return base

def package_verify_url(url: str) -> bool:
    req = Request(url, headers={
        "Range": "bytes=0-0",
        "Accept": "application/octet-stream",
    })

    try:
        with urlopen(req, timeout=30) as resp:
            if resp.status != 200 and resp.status != 206:
                raise ValueError(f"Unexpected status {resp.status} while fetching {url}")
            return True
    except HTTPError as err:
        if err.code == 404:
            return False
        raise


def package_get_url(package_name: str, version: str, package_filename: str, sources: list[str], base_url_cache: dict[str, str]):
    for source in sources:
        base_url = package_source_get_base_address(source, base_url_cache)
        url = "{}/{}/{}/{}".format(base_url, package_name, version, package_filename)
        if package_verify_url(url):
            return url
    raise f"Could not find URL for package '{package_name}' version '{version}' ('f{package_filename}')"

def dotnet_args(args: argparse.Namespace, runtime: str | None, cmd: str, cwd: str | None=None) -> list[str]:
    cwd_args = [f"--cwd={cwd}"] if cwd is not None else []
    return [
         "flatpak",
        "run"] + cwd_args + [
        "--env=DOTNET_CLI_TELEMETRY_OPTOUT=true",
        "--env=DOTNET_SKIP_FIRST_TIME_EXPERIENCE=true",
        "--command=sh",
        f"--runtime=org.freedesktop.Sdk//{args.freedesktop}",
        "--share=network",
        "--filesystem=host",
        f"org.freedesktop.Sdk.Extension.dotnet{args.dotnet}//{args.freedesktop}",
        "-c",
        f'PATH="${{PATH}}:/usr/lib/sdk/dotnet{args.dotnet}/bin" LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/lib/sdk/dotnet{args.dotnet}/lib" exec {cmd}'
    ]

def get_package_sources(args: argparse.Namespace, projects: list[str], runtimes: list[str] | None) -> list[str]:
    if runtimes is None:
        runtimes = [None]
    sources = {}
    for project in projects:
        project_dir = Path(project).resolve().parent
        for runtime in projects:
            out = subprocess.run(dotnet_args(args, runtime, "dotnet nuget list source --format=Short", project_dir), check=True, capture_output=True)
            for entry in out.stdout.decode().strip().split('\n'):
                if entry.startswith('E '):
                    sources[entry[2:]] = None
    return list(sources)

def main() -> None:
    # Bump this to latest freedesktop runtime version.
    freedesktop_default = "24.08"
    # Bump this to an LTS dotnet version.
    dotnet_default = "8"

    parser = argparse.ArgumentParser()
    parser.add_argument("output", help="The output JSON sources file")
    parser.add_argument("project", nargs="+", help="The project file(s)")
    parser.add_argument(
        "--runtime",
        "-r",
        nargs="+",
        default=[None],
        help="The target runtime(s) to restore packages for",
    )
    parser.add_argument(
        "--freedesktop",
        "-f",
        help="The target version of the freedesktop sdk to use",
        default=freedesktop_default,
    )
    parser.add_argument(
        "--dotnet",
        "-d",
        help="The target version of dotnet to use",
        default=dotnet_default,
    )
    parser.add_argument(
        "--destdir",
        help="The directory the generated sources file will save sources to",
        default="nuget-sources",
    )
    parser.add_argument(
        "--only-arches", help="Limit the source to this Flatpak arch", default=None
    )
    parser.add_argument(
        "--dotnet-args",
        "-a",
        nargs=argparse.REMAINDER,
        help="Additional arguments to pass to the dotnet command",
    )
    args = parser.parse_args()

    sources = []
    package_sources = get_package_sources(args, args.project, args.runtime)
    base_url_cache = dict()
    with tempfile.TemporaryDirectory(dir=Path()) as tmp:
        def restore_project(project: str, runtime: str | None) -> None:
            out = subprocess.run(
                dotnet_args(args, runtime, 'dotnet restore "$@"') +
                [
                    "--",
                    "--packages",
                    tmp,
                    project,
                ]
                + (["-r", runtime] if runtime else [])
                + (args.dotnet_args or []),
                check=False,
            )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for project in args.project:
                if args.runtime:
                    for runtime in args.runtime:
                        futures.append(
                            executor.submit(restore_project, project, runtime)
                        )
                else:
                    futures.append(executor.submit(restore_project, project, None))
            concurrent.futures.wait(futures)

        print("done")

        for path in Path(tmp).glob("**/*.nupkg.sha512"):
            name = path.parent.parent.name
            version = path.parent.name
            filename = "{}.{}.nupkg".format(name, version)
            url = package_get_url(name, version, filename, package_sources, base_url_cache)

            with path.open() as fp:
                sha512 = binascii.hexlify(base64.b64decode(fp.read())).decode("ascii")

            data = {
                "type": "file",
                "url": url,
                "sha512": sha512,
                "dest": args.destdir,
                "dest-filename": filename,
            }

            if args.only_arches is not None:
                data["only-arches"] = [args.only_arches]

            sources.append(data)

    with open(args.output, "w", encoding="utf-8") as fp:
        json.dump(sorted(sources, key=lambda n: n.get("dest-filename")), fp, indent=4)


if __name__ == "__main__":
    main()
