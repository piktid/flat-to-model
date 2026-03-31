#!/usr/bin/env python3
"""
Minimal script to process a SKU folder with flat-to-model.

This script performs the basic workflow:
1. Create a project
2. Upload SKU/article images
3. Upload or verify identity
4. Build instructions (from CLI flags or JSON file)
5. Create flat-to-model job
6. Monitor job progress
7. Download results

Authentication uses an API token generated at https://app.on-model.com/profile?tab=tokens
"""

import argparse
import http.client
import json
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class FlatToModel:
    def __init__(self, base_url, token, input_folder, identity_code=None, identity_image=None, output_folder="output",
                 prompt=None, pose=None, background=None, num_variations=1, size=None, aspect_ratio=None, fmt=None, seed=None, instructions_file=None):
        self.base_url = base_url.rstrip("/")
        self.input_folder = Path(input_folder)
        self.identity_code = identity_code
        self.identity_image = Path(identity_image) if identity_image else None
        self.output_folder = Path(output_folder)

        # Instruction parameters (simple mode)
        self.prompt = prompt
        self.pose = pose
        self.background = background
        self.num_variations = num_variations
        self.size = size
        self.aspect_ratio = aspect_ratio
        self.fmt = fmt
        self.seed = seed

        # Advanced mode
        self.instructions_file = Path(instructions_file) if instructions_file else None

        self.access_token = token
        self.project_id = None
        self.project_name = None

    def get_auth_headers(self):
        """Get headers with Bearer token."""
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request_with_retry(self, method, url, max_retries=5, initial_delay=1.0, max_delay=60.0, **kwargs):
        """Make an authenticated request with retry on rate limiting (429).

        Args:
            method: HTTP method ('get', 'post', etc.)
            url: Full URL to request
            max_retries: Maximum retry attempts for 429 responses (default: 5)
            initial_delay: Initial backoff delay in seconds (default: 1.0)
            max_delay: Maximum delay between retries (default: 60.0)
            **kwargs: Additional arguments passed to requests (json, params, timeout, etc.)

        Returns:
            Response object
        """
        delay = initial_delay
        request_func = getattr(requests, method.lower())

        for attempt in range(max_retries + 1):
            headers = {**kwargs.pop("headers", {}), **self.get_auth_headers()}
            response = request_func(url, headers=headers, **kwargs)

            # Handle 401 - token expired or invalid
            if response.status_code == 401:
                print("Token expired or invalid. Generate a new one at https://app.on-model.com/profile?tab=tokens")
                return response

            # Not rate limited - return immediately
            if response.status_code != 429:
                return response

            # Rate limited (429) - retry with exponential backoff + jitter
            if attempt < max_retries:
                jitter = delay * 0.2 * (2 * random.random() - 1)
                wait_time = min(delay + jitter, max_delay)
                print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                delay = min(delay * 2, max_delay)
            else:
                print(f"Rate limited (429). Max retries ({max_retries}) exceeded.")

        return response

    def create_project(self, project_name):
        """Create a project on the API server."""
        print(f"Creating project '{project_name}'...")

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/project",
                json={"project_name": project_name}
            )

            if response.status_code in [200, 201]:
                data = response.json()
                self.project_id = data["project_id"]
                self.project_name = data["project_name"]
                print(f"Project created: {self.project_id}")
                return True
            elif response.status_code == 409:
                # Project already exists, get its ID from the response
                print(f"Project '{project_name}' already exists")
                data = response.json()
                if "project_id" in data:
                    self.project_id = data["project_id"]
                    self.project_name = project_name
                    print(f"Using existing project: {self.project_id}")
                    return True
                # Fallback: list projects to find the matching one
                return self._find_project_by_name(project_name)
            else:
                print(f"Failed to create project: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"Error creating project: {e}")
            return False

    def _find_project_by_name(self, project_name):
        """Find an existing project by name via the list endpoint."""
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/project",
                params={"per_page": 100}
            )
            if response.status_code == 200:
                data = response.json()
                for project in data.get("projects", []):
                    if project.get("project_text") == project_name:
                        self.project_id = project.get("project_key", project.get("project_id"))
                        self.project_name = project_name
                        print(f"Found existing project: {self.project_id}")
                        return True
            print(f"Could not find project '{project_name}'")
            return False
        except Exception as e:
            print(f"Error listing projects: {e}")
            return False

    def get_upload_url(self, filename):
        """Get a pre-signed upload URL for an image."""
        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/upload",
                json={"filename": filename}
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get upload URL: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error getting upload URL: {e}")
            return None

    def upload_image(self, upload_url, file_path, content_type):
        """Upload an image to S3 using the pre-signed URL."""
        try:
            with open(file_path, "rb") as f:
                image_data = f.read()

            parsed = urlparse(upload_url)

            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
            else:
                conn = http.client.HTTPConnection(parsed.netloc, timeout=120)

            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"

            headers = {"Content-Type": content_type}
            conn.request("PUT", path, body=image_data, headers=headers)
            response = conn.getresponse()
            response.read()  # Read response to complete request
            conn.close()

            return response.status in [200, 201]
        except Exception as e:
            print(f"Error uploading image: {e}")
            return False

    def upload_sku_images(self):
        """Upload all SKU/article images from the input folder."""
        if not self.input_folder.exists():
            print(f"Input folder not found: {self.input_folder}")
            print(f"Checked path: {self.input_folder.absolute()}")
            return []

        # Find all image files
        image_extensions = [".jpg", ".jpeg", ".png"]
        image_files = [
            f for f in sorted(self.input_folder.iterdir())
            if f.is_file() and f.suffix.lower() in image_extensions
        ]

        if not image_files:
            print(f"No images found in {self.input_folder}")
            return []

        print(f"Found {len(image_files)} SKU images to upload")

        # Create project if not already created
        if not self.project_name:
            project_name = self.input_folder.name
            if not self.create_project(project_name):
                return []

        file_ids = []

        for image_file in image_files:
            print(f"Uploading {image_file.name}...")

            upload_info = self.get_upload_url(image_file.name)
            if not upload_info:
                print(f"Failed to get upload URL for {image_file.name}")
                continue

            upload_url = upload_info["upload_url"]
            # Fix URL scheme if needed
            if self.base_url.startswith("https://") and upload_url.startswith("http://"):
                upload_url = upload_url.replace("http://", "https://", 1)

            success = self.upload_image(
                upload_url,
                image_file,
                upload_info["content_type"]
            )

            if success:
                file_ids.append(upload_info["file_id"])
                print(f"Uploaded: {image_file.name} -> {upload_info['file_id']}")
            else:
                print(f"Failed to upload: {image_file.name}")

        print(f"Successfully uploaded {len(file_ids)}/{len(image_files)} SKU images")
        return file_ids

    def get_or_upload_identity(self):
        """Get identity code from existing identity or upload new one."""
        if self.identity_code:
            # Check if identity exists
            try:
                response = self._request_with_retry(
                    "get",
                    f"{self.base_url}/identity/{self.identity_code}"
                )
                if response.status_code == 200:
                    data = response.json()
                    print(f"Using existing identity: {self.identity_code}")
                    return data["identity_code"]
            except Exception as e:
                print(f"Error checking identity: {e}")

        if self.identity_image and self.identity_image.exists():
            # Upload new identity
            print(f"Uploading identity image: {self.identity_image.name}...")

            try:
                # Detect content type from file extension
                suffix = self.identity_image.suffix.lower()
                content_type_map = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png"
                }
                content_type = content_type_map.get(suffix, "image/jpeg")

                # Retry loop for multipart upload (file must be re-opened each attempt)
                delay = 1.0
                max_retries = 5
                for attempt in range(max_retries + 1):
                    with open(self.identity_image, "rb") as f:
                        files = {"image": (self.identity_image.name, f, content_type)}
                        form_data = {"name": self.identity_image.stem}

                        response = requests.post(
                            f"{self.base_url}/identity/upload",
                            headers=self.get_auth_headers(),
                            files=files,
                            data=form_data,
                            timeout=30
                        )

                    if response.status_code == 429 and attempt < max_retries:
                        jitter = delay * 0.2 * (2 * random.random() - 1)
                        wait_time = min(delay + jitter, 60.0)
                        print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}...")
                        time.sleep(wait_time)
                        delay = min(delay * 2, 60.0)
                        continue
                    break

                if response.status_code in [200, 201]:
                    identity_data = response.json()
                    identity_code = identity_data["identity_code"]
                    print(f"Identity uploaded: {identity_code}")
                    return identity_code
                elif response.status_code == 409:
                    # Identity already exists
                    identity_data = response.json()
                    identity_code = identity_data.get("existing_identity_code", identity_data.get("identity_code"))
                    print(f"Identity already exists: {identity_code}")
                    return identity_code
                else:
                    print(f"Failed to upload identity: {response.status_code}")
                    print(f"Response: {response.text}")
                    return None
            except Exception as e:
                print(f"Error uploading identity: {e}")
                return None

        print("No identity code or identity image provided")
        return None

    def _build_instructions(self):
        """Build the instructions array from CLI flags or a JSON file.

        Advanced mode (--instructions-file): Load instructions from a JSON file.
        Simple mode (CLI flags): Build a single instruction from individual flags.

        Returns:
            list[dict]: Instructions array to send to the API.
        """
        # Advanced mode: load from JSON file
        if self.instructions_file:
            if not self.instructions_file.exists():
                print(f"Instructions file not found: {self.instructions_file}")
                return None
            try:
                with open(self.instructions_file, "r") as f:
                    data = json.load(f)
                # Accept either a list or a dict with an "instructions" key
                if isinstance(data, list):
                    instructions = data
                elif isinstance(data, dict) and "instructions" in data:
                    instructions = data["instructions"]
                else:
                    print("Invalid instructions file: expected a list or {\"instructions\": [...]}")
                    return None
                print(f"Loaded {len(instructions)} instructions from {self.instructions_file.name}")
                return instructions
            except json.JSONDecodeError as e:
                print(f"Error parsing instructions file: {e}")
                return None

        # Simple mode: build a single instruction from CLI flags
        instruction = {}

        if self.prompt:
            instruction["prompt"] = self.prompt
        if self.pose:
            instruction["pose"] = self.pose
        if self.background:
            instruction["background"] = self.background
        if self.seed is not None:
            instruction["seed"] = self.seed
        if self.num_variations and self.num_variations > 1:
            instruction["num_variations"] = self.num_variations

        options = {}
        if self.size:
            options["size"] = self.size
        if self.aspect_ratio:
            options["ar"] = self.aspect_ratio
        if self.fmt:
            options["format"] = self.fmt
        if options:
            instruction["options"] = options

        return [instruction]

    def create_job(self, identity_code, file_ids, instructions):
        """Create a flat-to-model job."""
        print("Creating flat-to-model job...")

        payload = {
            "project_id": self.project_id,
            "images": file_ids,
            "identity_code": identity_code,
            "instructions": instructions
        }

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/flat-2-model",
                json=payload
            )

            if response.status_code == 202:
                data = response.json()
                job_id = data["job_id"]
                total_outputs = data.get("total_outputs", len(instructions))
                print(f"Job created: {job_id} ({total_outputs} outputs)")
                return job_id
            else:
                print(f"Failed to create job: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error creating job: {e}")
            return None

    def wait_for_job(self, job_id, max_wait_time=1200, check_interval=5):
        """Wait for job to complete."""
        print("Waiting for job to complete...")

        start_time = time.time()
        max_retries_404 = 10
        retry_count = 0

        time.sleep(5)  # Initial delay

        while True:
            if time.time() - start_time > max_wait_time:
                print(f"Timeout: Job took longer than {max_wait_time} seconds")
                return None

            try:
                response = self._request_with_retry(
                    "get",
                    f"{self.base_url}/jobs/{job_id}/status"
                )

                if response.status_code == 404:
                    retry_count += 1
                    if retry_count <= max_retries_404:
                        print(f"Job not found yet (attempt {retry_count}/{max_retries_404}), waiting...")
                        time.sleep(30)
                        continue
                    else:
                        print(f"Job {job_id} not found after {max_retries_404} retries")
                        return None

                if response.status_code != 200:
                    print(f"Failed to get status: {response.status_code}")
                    print(f"Response: {response.text}")
                    return None

                retry_count = 0
                status_data = response.json()
                status = status_data["status"]
                progress = status_data.get("progress", 0)

                print(f"Progress: {progress:.1f}% - Status: {status}")

                if status in ["completed", "failed", "aborted"]:
                    print(f"Job finished with status: {status}")
                    return status

                time.sleep(check_interval)
            except Exception as e:
                print(f"Error checking status: {e}")
                time.sleep(check_interval)

    def download_results(self, job_id):
        """Download job results."""
        print("Downloading results...")

        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/jobs/{job_id}/results"
            )

            if response.status_code != 200:
                print(f"Failed to get results: {response.status_code}")
                print(f"Response: {response.text}")
                return False

            results_data = response.json()

            # Create output folder
            self.output_folder.mkdir(parents=True, exist_ok=True)

            # Download images
            if "results" in results_data:
                for result in results_data["results"]:
                    if result.get("status") == "completed":
                        output_url = None
                        if result.get("output") and isinstance(result["output"], dict):
                            output_url = result["output"].get("full_size")

                        if output_url:
                            try:
                                img_response = requests.get(output_url, timeout=30)
                                img_response.raise_for_status()

                                original_filename = result.get("original_filename", f"image_{result['image_index']}.jpg")
                                original_filename = Path(original_filename).name

                                version = result.get("version", 0)
                                if "." in original_filename:
                                    basename, ext = original_filename.rsplit(".", 1)
                                    filename = f"{basename}_v{version}.{ext}"
                                else:
                                    filename = f"{original_filename}_v{version}"

                                output_path = self.output_folder / filename
                                with open(output_path, "wb") as f:
                                    f.write(img_response.content)

                                print(f"Downloaded: {filename}")
                            except Exception as e:
                                print(f"Failed to download image {result['image_index']}: {e}")

            # Save metadata
            metadata_path = self.output_folder / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(results_data, f, indent=2)

            print(f"Results saved to {self.output_folder}")
            return True
        except Exception as e:
            print(f"Error downloading results: {e}")
            return False

    def run(self):
        """Run the complete workflow."""
        print("=" * 70)
        print("Flat-to-Model")
        print("=" * 70)

        # Step 1: Upload SKU images
        file_ids = self.upload_sku_images()
        if not file_ids:
            print("No images uploaded")
            return False

        if not self.project_id:
            print("No project ID available")
            return False

        # Step 3: Get or upload identity
        identity_code = self.get_or_upload_identity()
        if not identity_code:
            print("No identity code available")
            return False

        # Step 4: Build instructions
        instructions = self._build_instructions()
        if instructions is None:
            return False

        # Step 5: Create job
        job_id = self.create_job(identity_code, file_ids, instructions)
        if not job_id:
            return False

        # Step 6: Wait for completion
        status = self.wait_for_job(job_id)
        if status != "completed":
            print(f"Job did not complete successfully (status: {status})")
            return False

        # Step 7: Download results
        if not self.download_results(job_id):
            return False

        print("=" * 70)
        print("Processing complete")
        print("=" * 70)
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Script to generate on-model images from SKU/flat-lay photos"
    )
    parser.add_argument(
        "--input-folder",
        type=str,
        required=True,
        help="Path to folder containing SKU/article images"
    )
    parser.add_argument(
        "--identity-code",
        type=str,
        default=None,
        help="Existing identity code to use"
    )
    parser.add_argument(
        "--identity-image",
        type=str,
        default=None,
        help="Path to identity image file to upload"
    )
    parser.add_argument(
        "--output-folder",
        type=str,
        default="output",
        help="Output folder for results (default: output)"
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)"
    )
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token from https://app.on-model.com/profile?tab=tokens"
    )
    # Instruction flags (simple mode)
    instruction_group = parser.add_argument_group("instructions (simple mode)")
    instruction_group.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Text prompt describing the desired output"
    )
    instruction_group.add_argument(
        "--pose",
        type=str,
        default=None,
        help="Pose for the generated model (e.g., 'standing', 'walking')"
    )
    instruction_group.add_argument(
        "--background",
        type=str,
        default=None,
        help="Background description (e.g., 'white studio', 'urban street')"
    )
    instruction_group.add_argument(
        "--num-variations",
        type=int,
        default=1,
        help="Number of output variations per instruction (1-4, default: 1)"
    )
    instruction_group.add_argument(
        "--size",
        type=str,
        default=None,
        choices=["1K", "2K", "4K"],
        help="Output resolution (default: server default)"
    )
    instruction_group.add_argument(
        "--aspect-ratio",
        type=str,
        default=None,
        choices=["1:1", "3:4", "4:3", "9:16", "16:9"],
        help="Output aspect ratio (default: 1:1)"
    )
    instruction_group.add_argument(
        "--format",
        type=str,
        default=None,
        dest="fmt",
        choices=["png", "jpg"],
        help="Output image format (default: jpg)"
    )
    instruction_group.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed value for reproducibility"
    )

    # Advanced mode
    advanced_group = parser.add_argument_group("instructions (advanced mode)")
    advanced_group.add_argument(
        "--instructions-file",
        type=str,
        default=None,
        help="Path to JSON file with instructions array (overrides all simple flags)"
    )

    args = parser.parse_args()

    if not args.identity_code and not args.identity_image:
        parser.error("Either --identity-code or --identity-image must be provided")

    processor = FlatToModel(
        base_url=args.base_url,
        token=args.token,
        input_folder=args.input_folder,
        identity_code=args.identity_code,
        identity_image=args.identity_image,
        output_folder=args.output_folder,
        prompt=args.prompt,
        pose=args.pose,
        background=args.background,
        num_variations=args.num_variations,
        size=args.size,
        aspect_ratio=args.aspect_ratio,
        fmt=args.fmt,
        seed=args.seed,
        instructions_file=args.instructions_file,
    )

    success = processor.run()

    if not success:
        exit(1)


if __name__ == "__main__":
    main()
