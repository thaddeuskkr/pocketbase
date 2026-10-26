#!/usr/bin/env python3
"""Resolve an upstream release and decide whether its image tags need publishing."""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.github.com/repos/pocketbase/pocketbase/releases"
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
IMAGE = re.compile(rf"{COMPONENT}(?:/{COMPONENT})+\Z")
PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}


def github_release(suffix, token):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "pocketbase-docker"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}/{suffix}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"PocketBase release lookup failed: HTTP {error.code} ({suffix})") from error


def release_version(release):
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not VERSION.fullmatch(tag):
        raise ValueError(f"Expected a stable PocketBase release, received {tag!r}")
    return tag.removeprefix("v")


def release_tags(version, is_latest):
    tags = [version, f"v{version}"]
    if is_latest:
        major, minor, _ = version.split(".")
        tags += [f"{major}.{minor}", major, "latest"]
    return tags


def registry_config(env):
    event = env.get("GITHUB_EVENT_NAME", "")
    allowed = event in {"schedule", "workflow_dispatch"}
    ghcr = allowed and env.get("PUBLISH_GHCR", "").lower() != "false" and bool(env.get("GH_TOKEN"))
    hub = allowed and bool(env.get("DOCKERHUB_USERNAME") and env.get("DOCKERHUB_TOKEN"))
    ghcr_image = "ghcr.io/" + env.get("GITHUB_REPOSITORY", "").lower()
    hub_image = env.get("DOCKERHUB_IMAGE") or f"{env.get('DOCKERHUB_USERNAME', '')}/pocketbase"
    images = []
    if ghcr:
        images.append(ghcr_image)
    if hub:
        images.append(hub_image)
    for image in images:
        if not IMAGE.fullmatch(image):
            raise ValueError(f"Invalid lowercase image repository: {image!r}; omit tag and registry scheme")
    return ghcr, hub, ghcr_image, hub_image, images


def resolve(env, fetch=github_release):
    token = env.get("GH_TOKEN", "")
    requested = env.get("REQUESTED_VERSION", "").strip()
    if requested and not VERSION.fullmatch(requested):
        raise ValueError("Version must be a stable version like 0.40.6 or v0.40.6")
    latest = fetch("latest", token)
    latest_version = release_version(latest)
    selected = latest
    if requested and requested.removeprefix("v") != latest_version:
        selected = fetch(f"tags/v{requested.removeprefix('v')}", token)
    version = release_version(selected)
    if requested and version != requested.removeprefix("v"):
        raise ValueError("Release API returned a different version than requested")
    required = {"checksums.txt"} | {
        f"pocketbase_{version}_linux_{arch}.zip" for _, arch in PLATFORMS
    }
    missing = required - {asset["name"] for asset in selected.get("assets", [])}
    if missing:
        raise ValueError("Release is missing required assets: " + ", ".join(sorted(missing)))
    ghcr, hub, ghcr_image, hub_image, images = registry_config(env)
    tags = [f"{image}:{tag}" for image in images for tag in release_tags(version, version == latest_version)]
    force = env.get("FORCE_BUILD", "").lower() == "true" or env.get("GITHUB_EVENT_NAME") in {"push", "pull_request"}
    return {
        "version": version,
        "upstream_tag": selected["tag_name"],
        "is_latest": str(version == latest_version).lower(),
        "ghcr": str(ghcr).lower(),
        "dockerhub": str(hub).lower(),
        "ghcr_image": ghcr_image,
        "dockerhub_image": hub_image,
        "publish": str(bool(images)).lower(),
        "force": str(force).lower(),
        "tags": "\n".join(tags),
    }


def needs_build(tags, force=False, run=subprocess.run):
    if force or not tags:
        return True
    # Check every alias and registry. Partial publishes and deleted tags are repaired.
    manifests = {}
    for reference in tags:
        try:
            result = run(
                ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"Registry lookup timed out for {reference}; rebuilding", file=sys.stderr)
            return True
        if result.returncode:
            # A failed lookup must never be interpreted as a successful publication.
            print(f"Cannot confirm {reference} is published; rebuilding", file=sys.stderr)
            return True
        try:
            manifest = json.loads(result.stdout)
            platforms = {
                (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
                for item in manifest.get("manifests", [])
            }
        except (ValueError, AttributeError, TypeError):
            return True
        if not PLATFORMS <= platforms:
            return True
        image = reference.rsplit(":", 1)[0]
        # Registries can convert manifests differently; compare aliases within each registry.
        canonical = json.dumps(manifest, sort_keys=True)
        if image in manifests and manifests[image] != canonical:
            return True
        manifests[image] = canonical
    return False


def write_outputs(values):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a") as stream:
            for key, value in values.items():
                delimiter = uuid.uuid4().hex
                stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    # Outputs contain configuration only, never credentials.
    print(json.dumps(values, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["resolve", "check"])
    args = parser.parse_args()
    if args.command == "resolve":
        write_outputs(resolve(os.environ))
    else:
        tags = os.environ.get("IMAGE_TAGS", "").splitlines()
        force = os.environ.get("FORCE_BUILD", "").lower() == "true"
        write_outputs({"build": str(needs_build(tags, force)).lower()})


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(str(error))
