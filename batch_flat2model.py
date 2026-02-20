#!/usr/bin/env python3
"""
Batch Flat-to-Model - Process multiple SKU folders in parallel.

This script wraps flat_to_model.py to process multiple SKU folders concurrently,
with a configurable parallelism level (default: 3, max: 5).

Each parallel worker runs a full independent workflow (auth, upload, job, download)
so there is no shared state between threads.

Usage:
    # Process all subfolders in a directory
    python batch_flat2model.py \
        --input-dir SKU/ \
        --username your_email@example.com \
        --password your_password \
        --identity-code PiktidPremium \
        --output-dir results/

    # Process specific folders with custom instructions
    python batch_flat2model.py \
        --input-folders SKU/ARTICLE1 SKU/ARTICLE2 SKU/ARTICLE3 \
        --username your_email@example.com \
        --password your_password \
        --identity-image identities/female/Lisa.jpg \
        --instructions-file instructions.json \
        --output-dir results/ \
        --parallel 5
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from flat_to_model import FlatToModel


def process_single_sku(base_url, username, password, input_folder, identity_code,
                       identity_image, output_folder,
                       prompt, pose, background, num_variations, size,
                       aspect_ratio, fmt, seed, instructions_file):
    """Process a single SKU folder. Runs in its own thread with its own FlatToModel instance."""
    start = time.time()

    processor = FlatToModel(
        base_url=base_url,
        username=username,
        password=password,
        input_folder=str(input_folder),
        identity_code=identity_code,
        identity_image=identity_image,
        output_folder=str(output_folder),
        prompt=prompt,
        pose=pose,
        background=background,
        num_variations=num_variations,
        size=size,
        aspect_ratio=aspect_ratio,
        fmt=fmt,
        seed=seed,
        instructions_file=instructions_file,
    )

    success = processor.run()
    elapsed = time.time() - start

    return {
        "folder": input_folder.name,
        "success": success,
        "processing_time": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Batch Flat-to-Model - Process multiple SKU folders in parallel"
    )

    # Input: either --input-dir (all subfolders) or --input-folders (specific paths)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input-dir",
        type=str,
        help="Directory containing SKU subfolders (each subfolder is processed as a separate job)",
    )
    input_group.add_argument(
        "--input-folders",
        type=str,
        nargs="+",
        help="Specific SKU folder paths to process",
    )

    parser.add_argument(
        "--username", type=str, required=True, help="API username (required)"
    )
    parser.add_argument(
        "--password", type=str, required=True, help="API password (required)"
    )
    parser.add_argument(
        "--identity-code", type=str, default=None, help="Existing identity code to use"
    )
    parser.add_argument(
        "--identity-image", type=str, default=None, help="Path to identity image file to upload"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Base output directory — results saved to <output-dir>/<folder-name>/ (default: output)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Number of parallel workers (default: 3, max: 5)",
    )

    # Instruction flags (simple mode)
    instruction_group = parser.add_argument_group("instructions (simple mode)")
    instruction_group.add_argument(
        "--prompt", type=str, default=None, help="Text prompt describing the desired output"
    )
    instruction_group.add_argument(
        "--pose", type=str, default=None, help="Pose for the generated model"
    )
    instruction_group.add_argument(
        "--background", type=str, default=None, help="Background description"
    )
    instruction_group.add_argument(
        "--num-variations", type=int, default=1, help="Number of output variations (1-4, default: 1)"
    )
    instruction_group.add_argument(
        "--size", type=str, default=None, choices=["1K", "2K", "4K"], help="Output resolution"
    )
    instruction_group.add_argument(
        "--aspect-ratio", type=str, default=None, choices=["1:1", "3:4", "4:3", "9:16", "16:9"],
        help="Output aspect ratio"
    )
    instruction_group.add_argument(
        "--format", type=str, default=None, dest="fmt", choices=["png", "jpg"], help="Output format"
    )
    instruction_group.add_argument(
        "--seed", type=int, default=None, help="Seed value for reproducibility"
    )

    # Advanced mode
    advanced_group = parser.add_argument_group("instructions (advanced mode)")
    advanced_group.add_argument(
        "--instructions-file", type=str, default=None,
        help="Path to JSON file with instructions array (overrides all simple flags)"
    )

    args = parser.parse_args()

    if not args.identity_code and not args.identity_image:
        parser.error("Either --identity-code or --identity-image must be provided")

    # Cap parallelism at 5 to respect API rate limits
    parallel = max(1, min(args.parallel, 5))

    # Collect input folders
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.exists():
            print(f"Input directory not found: {input_dir}")
            exit(1)
        folders = sorted([f for f in input_dir.iterdir() if f.is_dir()])
    else:
        folders = [Path(f) for f in args.input_folders]
        missing = [f for f in folders if not f.exists()]
        if missing:
            for f in missing:
                print(f"Folder not found: {f}")
            exit(1)

    if not folders:
        print("No folders to process")
        exit(1)

    output_dir = Path(args.output_dir)

    print("=" * 70)
    print("Batch Flat-to-Model")
    print("=" * 70)
    print(f"  Folders to process: {len(folders)}")
    print(f"  Parallel workers:   {parallel}")
    print(f"  Output directory:   {output_dir}")
    print(f"  API base URL:       {args.base_url}")
    if args.instructions_file:
        print(f"  Instructions file:  {args.instructions_file}")
    print("=" * 70)

    for i, folder in enumerate(folders, 1):
        print(f"  {i}. {folder.name}")
    print()

    start_time = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        future_to_folder = {}

        for folder in folders:
            per_folder_output = output_dir / folder.name
            future = executor.submit(
                process_single_sku,
                args.base_url,
                args.username,
                args.password,
                folder,
                args.identity_code,
                args.identity_image,
                per_folder_output,
                args.prompt,
                args.pose,
                args.background,
                args.num_variations,
                args.size,
                args.aspect_ratio,
                args.fmt,
                args.seed,
                args.instructions_file,
            )
            future_to_folder[future] = folder.name

        for future in as_completed(future_to_folder):
            folder_name = future_to_folder[future]
            try:
                result = future.result()
                results.append(result)
                status = "OK" if result["success"] else "FAILED"
                print(
                    f"\n[{len(results)}/{len(folders)}] {folder_name}: {status}"
                    f" ({result['processing_time']}s)"
                )
            except Exception as e:
                results.append({"folder": folder_name, "success": False, "error": str(e)})
                print(f"\n[{len(results)}/{len(folders)}] {folder_name}: ERROR - {e}")

    # Summary
    elapsed = time.time() - start_time
    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful

    print(f"\n{'=' * 70}")
    print("Summary")
    print(f"{'=' * 70}")
    print(f"  Total:      {len(results)}")
    print(f"  Successful: {successful}")
    print(f"  Failed:     {failed}")
    print(f"  Time:       {elapsed:.1f}s ({elapsed / 60:.1f} minutes)")

    if failed > 0:
        print(f"\n  Failed folders:")
        for r in results:
            if not r["success"]:
                error = r.get("error", "see console output above")
                print(f"    - {r['folder']}: {error}")

    print(f"{'=' * 70}")

    # Save batch summary
    summary_file = output_dir / f"batch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_file, "w") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "configuration": {
                    "parallel": parallel,
                    "base_url": args.base_url,
                    "identity_code": args.identity_code,
                    "identity_image": args.identity_image,
                    "instructions_file": args.instructions_file,
                },
                "total_folders": len(results),
                "successful": successful,
                "failed": failed,
                "total_time_seconds": round(elapsed, 1),
                "results": results,
            },
            f,
            indent=2,
        )
    print(f"Batch summary saved to {summary_file}")

    if failed > 0:
        exit(1)


if __name__ == "__main__":
    main()
