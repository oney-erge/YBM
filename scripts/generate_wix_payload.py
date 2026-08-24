"""Generate MSI components for a staged per-user release payload.

WiX's convenient ``<Files>`` wildcard creates file-keyed components. Windows
Installer's ICE38 validation rejects those below a user-profile directory: a
per-user component must use an HKCU registry value as its key path. Generate
the repetitive authoring so every packaged file has a stable component id and
an HKCU key path, without checking hundreds of generated lines into Git.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET
import uuid


WIX_NS = "http://wixtoolset.org/schemas/v4/wxs"
REGISTRY_KEY = r"Software\oney-erge\YBM\Components"
COMPONENT_NAMESPACE = uuid.UUID("0fcd92d0-04e7-49fb-a73c-44b8de24c318")


def _id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _guid(value: str) -> str:
    """Return the stable component GUID required for file + registry components."""
    return str(uuid.uuid5(COMPONENT_NAMESPACE, value)).upper()


def _element(parent: ET.Element, name: str, **attributes: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{WIX_NS}}}{name}", attributes)


def generate(payload_dir: Path, output: Path) -> None:
    payload_dir = payload_dir.resolve()
    files = sorted(path for path in payload_dir.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"payload is empty: {payload_dir}")

    ET.register_namespace("", WIX_NS)
    root = ET.Element(f"{{{WIX_NS}}}Wix")
    tree_fragment = _element(root, "Fragment")
    install_ref = _element(tree_fragment, "DirectoryRef", Id="INSTALLFOLDER")

    directories: dict[tuple[str, ...], ET.Element] = {(): install_ref}
    component_ids: list[str] = []

    def ensure_directory(parts: tuple[str, ...]) -> ET.Element:
        if parts in directories:
            return directories[parts]
        parent = ensure_directory(parts[:-1])
        relative = "/".join(parts)
        directory = _element(parent, "Directory", Id=_id("dir", relative), Name=parts[-1])
        directories[parts] = directory
        return directory

    for path in files:
        relative = path.relative_to(payload_dir).as_posix()
        parts = tuple(Path(relative).parts[:-1])
        parent = ensure_directory(parts)
        component_id = _id("cmp", relative)
        component_ids.append(component_id)
        component = _element(parent, "Component", Id=component_id, Guid=_guid(f"file:{relative}"))
        _element(component, "File", Id=_id("fil", relative), Source=str(path), KeyPath="no")
        _element(
            component,
            "RegistryValue",
            Root="HKCU",
            Key=REGISTRY_KEY,
            Name=_id("file", relative),
            Type="integer",
            Value="1",
            KeyPath="yes",
        )

    # Profile directories also need explicit uninstall ownership (ICE64).
    # RemoveFolder only removes an empty directory, so generated user state is
    # preserved while package-owned empty directories disappear.
    for parts, directory in sorted(directories.items(), key=lambda item: item[0]):
        relative = "/".join(parts) or "."
        component_id = _id("dircmp", relative)
        component_ids.append(component_id)
        component = _element(directory, "Component", Id=component_id, Guid=_guid(f"directory:{relative}"))
        directory_id = "INSTALLFOLDER" if not parts else directory.attrib["Id"]
        _element(component, "RemoveFolder", Id=_id("rm", relative), Directory=directory_id, On="uninstall")
        _element(
            component,
            "RegistryValue",
            Root="HKCU",
            Key=REGISTRY_KEY,
            Name=_id("directory", relative),
            Type="integer",
            Value="1",
            KeyPath="yes",
        )

    group_fragment = _element(root, "Fragment")
    group = _element(group_fragment, "ComponentGroup", Id="PayloadFiles")
    for component_id in component_ids:
        _element(group, "ComponentRef", Id=component_id)

    ET.indent(root, space="  ")
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    generate(args.payload_dir, args.output)
    print(f"Generated MSI payload authoring: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
