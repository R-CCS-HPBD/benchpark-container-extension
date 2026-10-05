# Benchpark Container Extension

Benchpark Container Extension adds **explicit, reproducible container execution** to an existing Benchpark installation while preserving Benchpark's native Spack workflow.

Container execution is opt-in:

```text
Benchpark
├── experiment without +container  -> existing Benchpark / Spack workflow
└── experiment with    +container  -> Benchpark Container Extension
```

Installing this package does **not** make containers the default and does not redirect existing non-container experiments.

The public repository contains the generic container-execution extension only. AI benchmark/model resources such as ResNet, vLLM, datasets, model weights, and model-specific overlays are intentionally maintained separately.

## Current scope

The extension currently provides:

- Apptainer, SingularityCE, and Docker runtime backends
- per-System runtime capability declaration and default runtime selection
- per-Experiment runtime override
- reusable immutable container releases through a Container Catalog
- retained SIF and OCI content through a Managed Artifact Store
- run-local pinned Python requirements and setup scripts without modifying the managed base image
- normal Benchpark/Ramble execution through `experiment init -> setup -> ramble`
- Container Execution Records (CERs)
- CER v1 hardware and container provenance collection
- NVIDIA GPU discovery through `nvidia-smi`
- AMD GPU discovery through `amd-smi`, with `rocminfo` fallback
- hashed CER evidence files and recorded-file integrity validation
- explicit runtime/image identity checks with no silent runtime fallback
- coexistence with the existing Benchpark / Spack execution path

This repository does **not** provide:

- AI benchmark/model implementations
- model weights or datasets
- container images
- GPU drivers
- time-series GPU/CPU/resource profiling
- automatic tuning
- automatic Managed Artifact garbage collection
- container signature verification
- site scheduler configuration

CER provenance collection and performance profiling are intentionally separate concerns. CER records identity, configuration, selected observations, and evidence for an execution attempt; it is not a time-series profiler.

---

# Requirements

## Benchpark

This project extends an existing Benchpark checkout through a small generic external-plugin seam.

A Benchpark checkout that already contains a compatible generic plugin seam does not need to be patched again. For a checkout without the seam, generate and review the integration patch with `tools/patch_core.py` before applying it.

## Python

```text
Python >= 3.10
```

Use the same Python environment that runs Benchpark.

## Supported execution hosts

Container execution and runtime acceptance are intended for Linux systems.

The current runtime/platform model uses Linux container platforms such as:

```text
linux/amd64
linux/arm64
```

Host-only source inspection, packaging, or some unit tests may be possible elsewhere, but that is not equivalent to Linux runtime acceptance.

## Container runtime

Install at least one runtime that the target System will use:

- Apptainer
- SingularityCE
- Docker

## OCI import

Managed OCI registration uses the host `skopeo` executable:

```bash
skopeo --version
```

`skopeo` is not required when only local SIF artifacts are registered.

## GPU execution

GPU drivers and runtime-specific GPU passthrough must already work on the host.

The extension does not install or configure NVIDIA/AMD drivers, Docker GPU support, ROCm, or site-specific device permissions.

---

# Repository layout

A typical source layout keeps Benchpark and its extensions separate:

```text
benchpark-workspace/
├── benchpark/
├── benchpark-extensions/
│   └── benchpark-container-extension/
└── .venv/
```

The exact parent directories are not significant. What matters is that the Benchpark source tree and Container Extension source tree remain separate.

The public Container Extension repository contains the generic extension implementation:

```text
benchpark-container-extension/
├── core/       # generic Benchpark integration seam used by the patch generator
├── examples/   # generic container examples
├── src/        # installable extension packages and runtime resources
├── tests/      # current public functional/regression tests
├── tools/      # patch, package, runtime, audit, and validation tools
├── LICENSE
├── NOTICE
├── MANIFEST.in
├── pyproject.toml
└── README.md
```

Historical source snapshots, internal review records, authorization files, generated acceptance reports, and AI-specific resources are not part of the public repository.

---

# Install the extension

## 1. Clone Benchpark and the extension separately

For example:

```bash
mkdir -p ~/src
cd ~/src

git clone <BENCHPARK_REPOSITORY>
git clone https://github.com/R-CCS-HPBD/benchpark-container-extension.git
```

Record the exact revisions used for reproducibility:

```bash
git -C ~/src/benchpark rev-parse HEAD
git -C ~/src/benchpark-container-extension rev-parse HEAD
```

## 2. Apply the generic Core seam only when required

If the target Benchpark checkout does not already contain the compatible generic external-plugin seam:

```bash
cd ~/src/benchpark-container-extension

python tools/patch_core.py   ~/src/benchpark   --output /tmp/benchpark-container-extension.patch
```

Review it:

```bash
git -C ~/src/benchpark apply   --stat   /tmp/benchpark-container-extension.patch

git -C ~/src/benchpark apply   --check   /tmp/benchpark-container-extension.patch
```

Apply it only after review:

```bash
git -C ~/src/benchpark apply   /tmp/benchpark-container-extension.patch
```

`pip install` does **not** modify the Benchpark source tree.

The Core patch is intentionally generic. Container runtimes, Catalog logic, CER implementation, and collectors remain in this external repository.

## 3. Install into the Benchpark Python environment

Activate the same virtual environment used to run Benchpark:

```bash
source /path/to/benchpark-venv/bin/activate

which python
which benchpark
```

Then install the extension:

```bash
cd ~/src/benchpark-container-extension
python -m pip install .
```

For extension development:

```bash
python -m pip install -e '.[test]'
```

Verify discovery:

```bash
benchpark --help
benchpark container --help
benchpark cer --help
```

Installing the package into an unrelated Python environment can prevent Benchpark from discovering the extension entry points.

---

# Execution model

The responsibilities are intentionally separated.

```text
Benchpark Core
      |
      | generic external plugin seam
      v
Benchpark Container Extension
      |
      +-- System
      |     +-- available runtimes
      |     +-- default runtime
      |     +-- platform
      |     +-- GPU passthrough
      |
      +-- Experiment
      |     +-- workload intent
      |     +-- logical image/release selection
      |     +-- run-local additions
      |
      +-- Container Catalog
      |     +-- immutable logical releases
      |     +-- retained SIF / OCI content
      |
      +-- Runtime backend
      |     +-- Apptainer
      |     +-- SingularityCE
      |     +-- Docker
      |
      +-- CER
            +-- resolved state
            +-- observed state
            +-- hardware/container provenance
            +-- evidence hashes
            +-- attempt result
```

## System responsibility

A System describes machine/runtime capability:

- available container runtimes
- runtime executable for each declared runtime
- default runtime
- Linux container platform
- GPU passthrough mode
- runtime cache/execution settings
- artifact roots required by experiments

A System does not own the reusable image inventory.

## Experiment responsibility

An Experiment describes benchmark intent:

- workload and parameters
- logical container image and immutable release
- pinned requirements
- setup scripts
- mounts and environment
- benchmark artifacts

The Experiment is not tied to a single runtime when the selected Catalog release provides compatible runtime artifacts.

## Catalog responsibility

The Container Catalog owns reusable retained base environments:

- logical image name
- immutable release
- platform
- accelerator family
- runtime bindings
- retained SIF or OCI content
- source provenance
- managed artifact identity

---

# Container Catalog

The Catalog is more than a list of source URIs. Registration retains the artifact content used for later execution.

The default visible registration file is:

```text
~/benchpark-containers/catalogs.yaml
```

A different registration file can be selected with:

```bash
export BPCE_CONFIG="$HOME/benchpark-containers/catalogs.yaml"
```

## Create and register a Catalog

```bash
benchpark container catalog init   "$HOME/benchpark-containers/personal"   --name personal

benchpark container catalog add   "$HOME/benchpark-containers/personal"   --name personal
```

List registered Catalogs:

```bash
benchpark container catalog list
```

## Register a SIF release

```bash
benchpark container register   --catalog personal   --name common-base   --release r1   --kind sif   --source /absolute/path/to/common-base.sif   --sha256 <SIF_SHA256>   --platform linux/arm64   --accelerator nvidia   --runtime apptainer   --runtime singularity   --python python3   --shell bash
```

Inspect and validate:

```bash
benchpark container list

benchpark container show   personal:common-base   --release r1

benchpark container validate   personal:common-base   --release r1
```

## Register an OCI release

```bash
benchpark container register   --catalog personal   --name common-docker-base   --release r1   --kind oci   --source registry.example.org/team/base@sha256:<SOURCE_DIGEST>   --platform linux/amd64   --accelerator nvidia   --runtime docker   --python python3   --shell bash
```

Managed OCI registration imports the required OCI content into filesystem-managed storage. Later execution uses the retained content instead of depending on a pre-existing Docker daemon image.

An explicit tag may be accepted as registration input when the importer resolves it to an immutable digest during registration. Execution does not re-resolve the tag.

Do not rely on an implicit `latest`.

## Immutable releases

`name + release` is immutable.

For example:

```text
common-base / r1
common-base / r2
```

If the base environment changes, create a new release rather than rewriting an existing release.

## SIF retention

A SIF is copied into managed content-addressed storage and verified by hash.

```text
source.sif
   |
   | register
   v
Managed Artifact Store
   |
   +-- retained SIF content
```

After successful registration, the original source path is provenance rather than a required execution dependency.

## OCI identity

For OCI, source identity and retained managed identity are distinct concepts:

```text
origin / source_digest    -> source provenance
identity / stored_digest  -> retained managed representation
```

The extension verifies the integrity and closure of its managed OCI representation. It does not claim byte-for-byte or semantic equivalence between every possible source-registry representation and the normalized retained representation.

---

# Runtime selection

A System may expose multiple runtimes:

```text
available:
  - apptainer
  - singularity
  - docker

default:
  apptainer
```

An Experiment may override the System default:

```text
Experiment A -> apptainer
Experiment B -> docker
Experiment C -> System default
```

Selection precedence is:

```text
explicit experiment container_runtime
             |
             v
       selected runtime

otherwise

System default_runtime
```

If the selected runtime is unavailable, has an unexpected identity, or cannot use the selected artifact, execution fails explicitly.

The extension does **not** silently fall back to another runtime.

---

# Normal Benchpark / Ramble workflow

Container support does not add a separate container-only setup workflow.

Container use is selected in the Experiment specification:

```bash
benchpark experiment init   --dest=smoke-apptainer   /path/to/initialized-system   'common-base-smoke +container container_runtime=apptainer container_image=personal:common-base container_release=r1'
```

Then use the normal Benchpark workflow:

```bash
benchpark setup   /path/to/initialized-system/smoke-apptainer   /path/to/runs
```

Load the generated environment:

```bash
source /path/to/runs/setup.sh
```

Run the Ramble workspace:

```bash
WS=/path/to/runs/system/smoke-apptainer/workspace

ramble --workspace-dir "$WS" workspace setup
ramble --workspace-dir "$WS" on
ramble --workspace-dir "$WS" workspace analyze --formats json
```

The execution flow remains:

```text
benchpark system init
        |
benchpark experiment init
        |
benchpark setup
        |
source <runs>/setup.sh
        |
ramble workspace setup
        |
ramble on
        |
ramble workspace analyze
```

Experiments without `+container` remain on the native Benchpark / Spack path.

---

# Run-local environment

Managed base containers are treated as immutable reusable bases.

Experiment-specific additions are prepared separately for a run, including:

```text
/bpce/python
/bpce/tools
```

The container's own declared Python, pip, and shell are used.

The extension does not inject an unrelated host Python environment into the container.

Typical run-local additions include:

- pinned Python requirements
- experiment setup scripts
- benchmark-specific helper tools

The managed base artifact remains unchanged.

If an addition becomes stable and reusable across experiments, create a new base image and Catalog release.

---

# Container Execution Record (CER)

A CER records one concrete container execution attempt.

Failed attempts are retained rather than overwritten.

CER identity is split deliberately:

- `condition_id` identifies the experiment condition
- `plan_sha256` identifies the frozen execution plan
- post-run collectors add evidence and observations without changing those identities
- the final record hash covers the completed CER record

## CER v1 collection

At run finalization, the extension executes independent collectors and merges their output additively into the CER.

Collectors must not overwrite an existing CER field. A collision or collector error is recorded as a collection error instead of silently replacing existing provenance.

Collector failure is **non-fatal to the benchmark result**. For example, a benchmark that completed successfully remains `COMPLETED` even when a hardware probe fails. The CER reports collection status such as `partial` so the missing provenance is explicit.

## Host and hardware provenance

The hardware collector records host information and accelerator information when available.

### NVIDIA

When `nvidia-smi` is available, CER collection can record information such as:

- GPU index
- GPU UUID
- PCI bus ID
- GPU name
- driver version
- reported memory
- a final post-execution state snapshot

The post-execution state may include values such as P-state, power limit, and current clocks when the driver exposes them.

### AMD

When `amd-smi` is available, CER collection uses JSON-oriented commands including:

```text
amd-smi version --json
amd-smi list --json
amd-smi static --json
amd-smi metric --json
```

If `amd-smi` is unavailable, `rocminfo` can be used as a fallback for accelerator identity information.

The collector is intended to work across AMD Instinct systems supported by the installed ROCm/AMD SMI stack; actual field availability depends on the installed driver/tool version and device.

### Important: snapshot, not telemetry

Dynamic fields collected by CER are **finalization-time observations**.

They are not a time series and must not be interpreted as resource utilization during the benchmark.

Continuous sampling of power, temperature, utilization, memory activity, clocks, or other performance/resource telemetry belongs to a separate profiling layer.

## Container provenance

The container collector summarizes the resolved and observed container execution state, including available information such as:

- selected runtime and runtime version
- runtime executable identity
- selected image kind and identity
- logical Catalog image/release information
- managed/unmanaged selection information
- GPU passthrough mode inferred from the concrete execution
- mount count
- declared execution-environment names

The collector uses already-resolved/observed execution state rather than inventing a second independent runtime selection.

## Evidence files

Raw collector command output is retained under the run's CER evidence area, for example:

```text
cer-evidence/
└── hardware/
    ├── nvidia/
    └── amd/
```

Recorded evidence files are hashed and added to the CER file inventory.

`benchpark cer validate` verifies the CER and recorded-file integrity and reports mismatches when recorded evidence has been modified after finalization.

## CER commands

List CERs:

```bash
benchpark cer list /path/to/workspace
```

Show one CER:

```bash
benchpark cer show /path/to/cer.json
```

Validate a CER and its recorded evidence:

```bash
benchpark cer validate /path/to/cer.json
```

Compare two CERs:

```bash
benchpark cer diff   /path/to/left/cer.json   /path/to/right/cer.json
```

Export an existing CER:

```bash
benchpark cer export   /path/to/cer.json   --output /path/to/exported-cer.json
```

CER validation establishes record/evidence integrity within the implemented scope. It does not claim scientific equivalence between benchmark runs.

---

# Reproducibility boundaries

Within its managed scope, the extension is designed to preserve these properties:

- existing Catalog `name + release` entries are immutable
- execution uses retained managed content instead of silently re-resolving a mutable source
- runtime selection is explicit and recorded
- selected image identity and Catalog release are recorded
- run-local additions do not modify the managed base artifact
- successful and failed attempts can coexist in CER history
- collector output does not rewrite the frozen condition or plan identity
- collector failures are visible instead of being silently ignored
- recorded CER evidence is hashed
- experiments without `+container` remain on the native Benchpark path

The extension does **not** by itself guarantee:

- identical performance across hardware
- identical behavior across different container images
- scientific equivalence between two runs
- permanent availability of external package repositories
- registry authentication
- third-party licensing rights
- container signature verification
- Managed Artifact Store backup
- protection from storage failure or administrator deletion
- site-specific scheduler/GPU configuration
- continuous resource telemetry
- benchmark tuning

Storage capacity, backup, retention, authentication, licensing, and site security remain operational responsibilities.

---

# Generic examples

The public repository contains only generic Container Extension examples.

`tools/configure_examples.py` can create a separate Benchpark configuration scope for those examples without copying the extension implementation into the upstream Benchpark source tree.

For example:

```bash
python tools/configure_examples.py   /path/to/benchpark   /tmp/bpce-example-scope   --bootstrap /path/to/benchpark-bootstrap
```

A generic System can then declare the available runtimes and a generic smoke Experiment can select a Catalog image/release.

AI benchmark/model definitions are intentionally outside this repository.

---

# Development and validation

Install development dependencies:

```bash
source /path/to/benchpark-venv/bin/activate
python -m pip install -e '.[test]'
```

Collect tests:

```bash
python -m pytest --collect-only -q
```

Run tests appropriate for the current host:

```bash
python -m pytest -q
```

Some runtime/GPU acceptance tests require the corresponding Linux runtime, driver, image, or benchmark environment. A non-Linux development host is not a substitute for Linux runtime acceptance.

Verify the packaged runtime worker:

```bash
python tools/build_runtime.py --check
```

Run the architecture audit:

```bash
python tools/audit_architecture.py
```

Build a wheel:

```bash
python -m pip wheel . --no-deps -w /tmp/bpce-wheel
```

When testing against a Benchpark checkout, generate a fresh integration patch for that checkout:

```bash
python tools/patch_core.py   /path/to/benchpark   --output /tmp/bpce-core.patch

git -C /path/to/benchpark apply --stat /tmp/bpce-core.patch
git -C /path/to/benchpark apply --check /tmp/bpce-core.patch
```

Do not edit generated Benchpark/Ramble workspace files to change experiment intent. Change the System or Experiment source definition and initialize a new instance.

---

# License

Licensed under the Apache License, Version 2.0.

See `LICENSE` and `NOTICE`.

---

# Acknowledgments

This work is based on results obtained from the project, “Research and Development Project of the Enhanced Infrastructures for Post-5G Information and Communication Systems” (JPNP25013), commissioned by the New Energy and Industrial Technology Development Organization (NEDO).
