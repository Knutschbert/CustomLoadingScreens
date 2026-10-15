import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path.cwd()
DATA_FILE = ROOT / "maps.json"
IMAGES_DIR = ROOT / "images"

MAX_IMAGE_SIZE = 15 * 1024 * 1024  # 15 MiB

ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

# GitHub-hosted attachment domains.
ALLOWED_IMAGE_HOSTS = {
    "github.com",
    "user-images.githubusercontent.com",
}


def fail(message):
    """Stop processing with a readable error."""
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def get_issue_field(body, label):
    """
    Extract an answer from a GitHub Issue Form body.

    GitHub renders issue form answers as Markdown headings, for example:

        ### Map identifier

        TDM_KP_Quarry
    """
    pattern = (
        rf"(?ims)^###\s+{re.escape(label)}\s*\n"
        rf"(.*?)(?=^###\s+|\Z)"
    )

    match = re.search(pattern, body)

    if not match:
        return ""

    value = match.group(1).strip()

    if value in {"_No response_", "No response"}:
        return ""

    return value


def validate_map_name(map_name):
    """Restrict map identifiers to safe, predictable characters."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", map_name):
        fail(
            "Map identifier is required and must contain only "
            "letters, numbers, underscores, and hyphens."
        )


def validate_texture_path(texture_path):
    """
    Validate a full Unreal object path.

    Example:
        /Game/UI/LoadingScreens/TO_HIPP.TO_HIPP
    """
    pattern = r"/Game/[A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+"

    if not re.fullmatch(pattern, texture_path):
        fail(
            "Invalid Unreal texture path. Provide a full object path, "
            "such as /Game/UI/LoadingScreens/TO_HIPP.TO_HIPP."
        )


def find_github_attachment(text):
    """
    Find an HTTPS image attachment URL from an allowed GitHub host.

    Arbitrary external URLs are rejected to reduce SSRF risk.
    """
    
    urls = re.findall(r"""https?://[^\s<>"'\])]+""", text)

    for raw_url in urls:
        url = raw_url.rstrip(".,\"'")
        print(f"Found URL: {url}")


    for raw_url in urls:
        url = raw_url.rstrip(".,")
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        allowed_host = (
            hostname in ALLOWED_IMAGE_HOSTS
            or hostname.endswith(".githubusercontent.com")
        )

        if parsed.scheme != "https" or not allowed_host:
            continue

        if parsed.username or parsed.password:
            continue

        return url

    return None


def download_image(url, map_name):
    """
    Download an image attachment and save it under images/.

    The extension is selected from the server's content type, not
    from a user-provided filename.
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        fail("GITHUB_TOKEN is not available.")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KendoPlus-MapSubmission/1.0",
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            final_parsed = urlparse(final_url)
            final_host = (final_parsed.hostname or "").lower()

            if (
                final_parsed.scheme != "https"
                or not (
                    final_host in ALLOWED_IMAGE_HOSTS
                    or final_host.endswith(".githubusercontent.com")
                )
            ):
                fail("Image URL redirected to an untrusted host.")

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if content_type not in ALLOWED_CONTENT_TYPES:
                fail(
                    f"Unsupported image content type: "
                    f"{content_type or 'unknown'}. "
                    "Use PNG, JPEG, or WebP."
                )

            payload = response.read(MAX_IMAGE_SIZE + 1)

    except SystemExit:
        raise
    except Exception as exc:
        fail(f"Could not download the image attachment: {exc}")

    if not payload:
        fail("The uploaded image is empty.")

    if len(payload) > MAX_IMAGE_SIZE:
        fail("Image exceeds the 15 MiB limit.")

    # Basic file-signature checks. Content-Type alone is not sufficient.
    signatures = {
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/webp": (
            len(payload) >= 12
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP"
        ),
    }

    if not signatures[content_type]:
        fail("The downloaded file does not match its declared image type.")

    extension = ALLOWED_CONTENT_TYPES[content_type]
    image_path = IMAGES_DIR / f"{map_name}{extension}"

    image_path.write_bytes(payload)

    return image_path.relative_to(ROOT).as_posix()


def load_json():
    if not DATA_FILE.exists():
        fail(f"Missing JSON data file: {DATA_FILE}")

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"maps.json is invalid JSON: {exc}")

    if not isinstance(data, dict):
        fail("maps.json must contain a JSON object.")

    if data.get("version") != 1:
        fail("maps.json must declare schema version 1.")

    if not isinstance(data.get("maps"), list):
        fail("maps.json must contain a 'maps' array.")

    return data


def main():
    body = os.environ.get("ISSUE_BODY", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")

    if not issue_number.isdigit():
        fail("Missing or invalid GitHub issue number.")

    # Read form answers.
    map_name = get_issue_field(body, "Map identifier")
    display_name = get_issue_field(body, "Display name")
    description = get_issue_field(body, "Map description")
    image_type = get_issue_field(body, "Image source")
    image_attachment = get_issue_field(body, "Image attachment")
    texture_path = get_issue_field(
        body,
        "Existing Unreal texture object path",
    )

    # Validate basic fields.
    validate_map_name(map_name)

    if len(display_name) > 160:
        fail("Display name must not exceed 160 characters.")

    if len(description) > 1000:
        fail("Description must not exceed 1000 characters.")

    data = load_json()
    maps = data["maps"]

    # Reject duplicates, case-insensitively.
    existing_names = {
        entry.get("map_name", "").casefold()
        for entry in maps
        if isinstance(entry, dict)
    }

    if map_name.casefold() in existing_names:
        fail(
            f"Map '{map_name}' already exists in maps.json. "
            "Ask a maintainer to update the existing entry."
        )

    # Resolve the selected image source.
    if image_type == "Existing Unreal texture":
        if image_attachment:
            fail(
                "Choose only one image source. "
                "Remove the image attachment when using an existing texture."
            )

        validate_texture_path(texture_path)

        image = {
            "type": "existing_texture",
            "path": texture_path,
        }

    elif image_type == "New uploaded image":
        if texture_path:
            fail(
                "Choose only one image source. "
                "Remove the texture path when submitting a new image."
            )

        attachment_url = find_github_attachment(image_attachment)

        if not attachment_url:
            fail(
                "No acceptable GitHub image attachment URL was found. "
                "Upload or paste an image into the Image attachment field."
            )

        relative_path = download_image(attachment_url, map_name)

        image = {
            "type": "file",
            "path": relative_path,
        }

    else:
        fail(
            "Image source must be 'New uploaded image' "
            "or 'Existing Unreal texture'."
        )

    # Add the new map entry.
    maps.append({
        "map_name": map_name,
        "display_name": display_name,
        "description": description,
        "image": image,
    })

    # Keep the file deterministic and easy to review.
    maps.sort(key=lambda entry: entry["map_name"].casefold())

    DATA_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Pass the branch name to subsequent GitHub Actions steps.
    branch_name = f"map-submission/issue-{issue_number}"

    github_output = os.environ.get("GITHUB_OUTPUT")

    if not github_output:
        fail("GITHUB_OUTPUT is not available.")

    with open(github_output, "a", encoding="utf-8") as output:
        output.write("changed=true\n")
        output.write(f"branch={branch_name}\n")

    print(f"Prepared map submission: {map_name}")
    print(f"Image source: {image['type']}")
    print(f"Image path: {image['path']}")


if __name__ == "__main__":
    main()