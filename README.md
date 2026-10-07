# Benchpark Container Extension

Benchpark Container Extension adds **opt-in container execution** to Benchpark while preserving the existing native Spack workflow.

```text
Benchpark experiment
├── without +container  -> native Benchpark / Spack
└── with    +container  -> Benchpark Container Extension
```

The extension is intentionally external to Benchpark Core. It provides container runtime selection, immutable container artifact management, run-local software additions, and Container Execution Records (CERs) without turning container execution into the default path.

> This repository contains the **generic container execution infrastructure only**.  
> Benchmark/model-specific resources such as ResNet, vLLM, datasets, model weights, and model-specific overlays are maintained separately.

## Features

- Apptainer, SingularityCE, and Docker backends
- explicit `+container` opt-in
- per-System runtime capability and default-runtime declaration
- per-Experiment runtime override
- immutable container releases through a Container Catalog
- retained SIF and OCI content through a Managed Artifact Store
- pinned run-local Python requirements and setup scripts
- normal Benchpark/Ramble execution flow
- CER generation for every container execution attempt
- NVIDIA hardware provenance through `nvidia-smi`
- AMD hardware provenance through `amd-smi`, with `rocminfo` fallback
- container/runtime/image provenance collection
- hashed CER evidence and integrity validation
- no silent runtime fallback
- no change to native Benchpark execution when `+container` is absent

## Repository layout

```text
benchpark-container-extension/
├── core/
│   └── files/                     # generic Benchpark plugin-seam payload
├── src/
│   ├── benchpark_container/       # container execution and CER
│   ├── benchpark_integration/     # Benchpark integration API
│   └── benchpark_tuning/          # tuning integration
├── tools/
│   └── patch_core.py              # generate Benchpark integration patch
├── LICENSE
├── MANIFEST.in
├── NOTICE
├── pyproject.toml
└── README.md
```

`core/` is **not** a copy of Benchpark. It contains the small generic
integration seam consumed by `tools/patch_core.py`.

`src/benchpark_container/resources/runtime.pyz` is the portable node-side
worker shipped with the extension.

## Architecture

```text
                    Benchpark Core
                         |
               generic plugin seam
                         |
                         v
            Benchpark Container Extension
                         |
        +----------------+----------------+
        |                |                |
      System         Experiment     Container Catalog
 runtime capability  workload       immutable releases
 default runtime     image/release  retained SIF/OCI
 platform/GPU        run-local env  artifact identity
        |                |                |
        +----------------+----------------+
                         |
                         v
                   Runtime backend
          Apptainer / Singularity / Docker
                         |
                         v
                      benchmark
                         |
                         v
                         CER
          resolved + observed + evidence
```

### System

A System describes what the machine can execute:

- available runtimes
- runtime executables
- default runtime
- Linux container platform
- GPU passthrough mode
- runtime cache/execution settings
- artifact roots required by experiments

A System does not own the reusable image inventory.

### Experiment

An Experiment describes what should be executed:

- workload and parameters
- logical image and immutable release
- pinned requirements
- setup scripts
- mounts and environment
- benchmark input/output artifacts

Container use is selected by adding `+container` to the normal Experiment specification.

### Container Catalog

The Catalog owns reusable immutable base environments:

- logical image name
- release
- platform
- accelerator family
- runtime bindings
- retained SIF or OCI content
- source provenance
- managed artifact identity

## Requirements

- Python 3.10 or newer
- an existing Benchpark checkout
- Linux for actual container execution
- at least one supported runtime:
  - Apptainer
  - SingularityCE
  - Docker
- `skopeo` when importing OCI images into managed storage
- working host GPU drivers/device access when GPU execution is required

The extension does not install GPU drivers, ROCm/CUDA, Docker GPU support, or site scheduler configuration.

## Installation

Benchpark and the extension should remain separate repositories.

A typical development layout is:

```text
benchpark-workspace/
├── benchpark/
├── benchpark-extensions/
│   └── benchpark-container-extension/
└── .venv/
```

### 1. Clone

```bash
git clone https://github.com/R-CCS-HPBD/benchpark-container-extension.git
cd benchpark-container-extension
```

Record the revision used:

```bash
git rev-parse HEAD
```

For reproducibility, record the Benchpark revision as well.

### 2. Ensure the Benchpark plugin seam exists

If the target Benchpark checkout does not already contain the compatible generic external-plugin seam:

```bash
python tools/patch_core.py \
  /path/to/benchpark \
  --output /tmp/benchpark-container-extension.patch
```

Review and verify it before applying:

```bash
git -C /path/to/benchpark apply \
  --stat \
  /tmp/benchpark-container-extension.patch

git -C /path/to/benchpark apply \
  --check \
  /tmp/benchpark-container-extension.patch
```

Apply only after review:

```bash
git -C /path/to/benchpark apply \
  /tmp/benchpark-container-extension.patch
```

Installing the Python package does **not** modify Benchpark automatically.

### 3. Install into the Benchpark Python environment

```bash
source /path/to/benchpark-venv/bin/activate
python -m pip install .
```

For development:

```bash
python -m pip install -e .
```

Verify discovery:

```bash
benchpark --help
benchpark container --help
benchpark cer --help
```

## Quick start

### Create a Catalog

The default visible Catalog registration file is:

```text
~/benchpark-containers/catalogs.yaml
```

A different registration file can be selected with `BPCE_CONFIG`.
Set it before creating or registering Catalogs:

```bash
export BPCE_CONFIG="$HOME/benchpark-containers/catalogs.yaml"
```

Create and register a Catalog:

```bash
benchpark container catalog init \
  "$HOME/benchpark-containers/personal" \
  --name personal

benchpark container catalog add \
  "$HOME/benchpark-containers/personal" \
  --name personal

benchpark container catalog list
```

The Catalog alias must be visible before registering container releases.

### Register a SIF release

```bash
SIF=/absolute/path/to/common-base.sif
SIF_SHA256=$(sha256sum "$SIF" | awk '{print $1}')

benchpark container register \
  --catalog personal \
  --name common-base \
  --release r1 \
  --kind sif \
  --source "$SIF" \
  --sha256 "$SIF_SHA256" \
  --platform linux/arm64 \
  --accelerator nvidia \
  --runtime apptainer \
  --python python3 \
  --shell bash
```

Inspect and validate the retained artifact:

```bash
benchpark container list

benchpark container show \
  personal:common-base \
  --release r1

benchpark container validate \
  personal:common-base \
  --release r1
```

### Connect an external benchmark repository

Benchmark-specific System, Experiment, and Ramble Application definitions
can be supplied by a separate repository.

```bash
python -m benchpark_integration.repository_install \
  --source-root /absolute/path/to/container-apps \
  --name container-apps \
  --display-name Container \
  --feature container \
  --benchpark-root /absolute/path/to/benchpark \
  --install
```

When an already connected repository is updated, refresh its baseline with
`--refresh-baseline`.

Use module execution as shown above. Do not execute
`repository_install.py` directly.

### Run a container-backed Experiment

Once a container-capable System and benchmark repository are available,
use the normal Benchpark workflow:

```bash
benchpark experiment init \
  --dest=my-container-experiment \
  /path/to/initialized-system \
  '<benchmark> +container container_runtime=apptainer container_image=personal:common-base container_release=r1'

benchpark setup \
  /path/to/initialized-system/my-container-experiment \
  /path/to/runs

source /path/to/runs/setup.sh
```

Then run the generated Ramble workspace normally.

There is no separate `benchpark container setup` workflow.

## Container Catalog and Managed Artifacts

A Catalog release is immutable: `name + release` identifies one logical retained base environment.

### SIF

Registered SIF content is copied into managed storage and verified by hash.

After successful registration, the original source path is provenance rather than an execution dependency.

### OCI

Managed OCI registration retains the required OCI content in filesystem-managed storage.

An explicit mutable tag may be accepted as registration input only when it is resolved and pinned during registration. Execution does not silently re-resolve the tag.

Do not rely on implicit `latest`.

For OCI artifacts, source identity and retained identity are intentionally separate:

```text
origin / source_digest    -> source provenance
identity / stored_digest  -> retained managed artifact
```

## Run-local software additions

The managed base artifact remains immutable.

Experiment-specific additions are prepared separately, including:

```text
/bpce/python
/bpce/tools
```

The base environment's declared Python, pip, and shell are used.

Typical additions include pinned requirements, setup scripts, and benchmark helper tools.

## Container Execution Record (CER)

A CER records one concrete container execution attempt.

Failed attempts are retained rather than overwritten.

### Identity

CER separates frozen execution identity from post-run observations:

- `condition_id` — experiment condition
- `plan_sha256` — frozen execution plan
- collector output — additive post-run observations/evidence
- `record_sha256` — finalized CER record hash

Collectors do not change `condition_id` or `plan_sha256`.

### Hardware provenance

At finalization, the hardware collector records available host and accelerator information.

For NVIDIA, `nvidia-smi` can provide:

- GPU UUID
- PCI bus ID
- GPU name
- driver version
- reported memory
- final P-state/power-limit/clock observations when available

For AMD, the collector uses `amd-smi` JSON commands when available and falls back to `rocminfo` for accelerator identity when needed.

Dynamic GPU values are **post-execution snapshots**, not time-series telemetry.

### Container provenance

The container collector summarizes the already-resolved/observed execution state, including:

- runtime name/version
- runtime executable identity
- image kind/identity
- logical Catalog image/release
- managed selection information
- GPU passthrough mode
- mounts
- declared execution-environment names

### Collector failures

Collector failures do not rewrite the benchmark verdict.

A benchmark that completed successfully remains completed even if a provenance collector fails. The CER records the collection error and can report a partial collection state.

Collectors are additive and cannot overwrite an existing CER field.

### Evidence integrity

Raw collector outputs are retained under the CER evidence area and included in the recorded file inventory with hashes.

```text
cer-evidence/
└── hardware/
    ├── nvidia/
    └── amd/
```

Validate a CER and its recorded evidence with:

```bash
benchpark cer validate /path/to/cer.json
```

Other CER commands:

```bash
benchpark cer list /path/to/workspace
benchpark cer show /path/to/cer.json

benchpark cer diff \
  /path/to/left/cer.json \
  /path/to/right/cer.json

benchpark cer export \
  /path/to/cer.json \
  --output /path/to/exported-cer.json
```

CER validation establishes record/evidence integrity within the implemented scope. It does not claim scientific equivalence between benchmark runs.

## Reproducibility boundaries

Within its managed scope, the extension is designed to preserve:

- immutable Catalog releases
- retained managed container content
- explicit runtime selection
- selected runtime/image identity
- frozen condition/plan identity
- run-local additions without base-image mutation
- separate successful and failed attempts
- additive CER observations
- hashed evidence
- native Benchpark behavior when `+container` is absent

The extension does not by itself guarantee:

- identical performance across machines
- scientific equivalence between runs
- identical behavior across different container images
- external package/registry availability
- third-party licensing rights
- container signature verification
- managed-store backup
- site scheduler/GPU configuration
- continuous resource telemetry
- automatic benchmark tuning

## License

Licensed under the Apache License, Version 2.0.

See `LICENSE` and `NOTICE`.

-----

# Acknowledgments

This work is based on results obtained from the project, “Research and Development Project of the Enhanced Infrastructures for Post-5G Information and Communication Systems” (JPNP25013), commissioned by the New Energy and Industrial Technology Development Organization (NEDO).
