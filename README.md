# Benchpark Container Extension

Benchpark Container Extension adds **reproducible container-based benchmark execution** to an existing Benchpark installation while preserving Benchpark's native Spack workflow.

Container execution is explicit and opt-in:

```text
Benchpark
├── experiment without +container  -> existing Benchpark / Spack workflow
└── experiment with    +container  -> Benchpark Container Extension
```

Installing this extension does **not** make container execution the default and does not automatically change existing experiments.

## Features

- Apptainer, SingularityCE, and Docker runtime backends
- per-experiment runtime selection
- reusable container environments through a Container Catalog
- a Managed Artifact Store that retains container content instead of only storing source URIs
- SIF and OCI image support
- run-local Python requirements and setup scripts without modifying the managed base image
- Container Execution Records (CERs) for resolved and observed execution state
- coexistence with the existing Benchpark / Spack execution path
- explicit runtime identity checks with no silent runtime fallback

## Requirements

### Benchpark

This project is an extension for an existing Benchpark checkout.

The Core integration generator is intentionally conservative. For a different Benchpark revision, always generate the patch and run `git apply --check` before applying it.

### Python

```text
Python >= 3.10
```

### Container runtime

Install at least one runtime you intend to use:

- Apptainer
- SingularityCE
- Docker

### OCI import

OCI registration uses the host `skopeo` executable:

```bash
skopeo --version
```

`skopeo` is not required when only local SIF artifacts are registered.

### GPU execution

GPU drivers and runtime-specific GPU passthrough must already work on the host.

For example, Docker GPU execution requires the host Docker configuration to expose the requested GPU devices to containers.

---

# Applying the Extension to an Existing Benchpark Installation

The recommended layout keeps Benchpark and this extension as separate Git repositories:

```text
~/src/
├── benchpark/
└── benchpark-container-extension/
```

Do not copy the extension source tree into the Benchpark repository.

## 1. Clone the extension

```bash
mkdir -p ~/src
cd ~/src

git clone https://github.com/<ORG>/benchpark-container-extension.git
cd benchpark-container-extension
```

Replace `<ORG>` with the GitHub organization or account that hosts this repository.

Record the exact extension revision used for the installation:

```bash
git rev-parse HEAD
```

For reproducibility, record both the Benchpark commit and the Benchpark Container Extension commit used for a benchmark environment.

```text
Benchpark repository
    commit A

Benchpark Container Extension repository
    commit B
```

The normal installation flow uses the checked-out repository state. A specific historical revision may be checked out when reproducing an older environment, but the standard installation procedure does not require a particular release tag.

## 2. Inspect the existing Benchpark checkout

Before modifying Benchpark, record its current revision and inspect local changes:

```bash
cd ~/src/benchpark

git status
git rev-parse HEAD
```

Preserve or reconcile any local modifications before applying the integration patch.

## 3. Generate the Benchpark Core integration patch

From the extension repository:

```bash
cd ~/src/benchpark-container-extension

python tools/patch_core.py \
  ~/src/benchpark \
  --output /tmp/benchpark-container-extension.patch
```

`patch_core.py` generates a reviewable unified diff and checks the target Benchpark layout.

By default, it does **not** modify the Benchpark checkout.

Installing the Python package also does **not** patch Benchpark automatically.

The `core/` directory in this repository contains the generic Benchpark integration seam used by the patch generator. Users should apply the generated patch rather than manually copying files from `core/`.

## 4. Review the patch

Inspect the files that would change:

```bash
git -C ~/src/benchpark apply \
  --stat \
  /tmp/benchpark-container-extension.patch
```

Verify that the patch applies cleanly:

```bash
git -C ~/src/benchpark apply \
  --check \
  /tmp/benchpark-container-extension.patch
```

If `git apply --check` fails, do not force the patch. The Benchpark revision or local modifications may need to be reconciled first.

## 5. Apply the patch

After reviewing it:

```bash
git -C ~/src/benchpark apply \
  /tmp/benchpark-container-extension.patch
```

Inspect the resulting checkout:

```bash
git -C ~/src/benchpark status --short
git -C ~/src/benchpark diff --stat
```

The Core patch adds a **generic external plugin seam**. Container runtimes, Catalog management, managed storage, and CER implementation remain in this external repository.

## 6. Install the extension

**Recommended:** use the same Python virtual environment that is used to run Benchpark.

The Benchpark source repository and the Benchpark Container Extension source repository remain separate, but their Python runtime environment is shared:

```text
Git repositories:
    separate

Python environment:
    shared
```

Activate the Benchpark virtual environment before installing the extension:

```bash
source /path/to/benchpark-venv/bin/activate

which python
which benchpark
```

Then install the extension from its repository:

```bash
cd ~/src/benchpark-container-extension

python -m pip install .
```

Installing into the Benchpark virtual environment is recommended because the extension is discovered through Python package entry points. Installing it into an unrelated Python environment can prevent Benchpark from discovering `+container`, `benchpark container`, and `benchpark cer`.

For extension development, use the same Benchpark virtual environment and install the checkout in editable mode:

```bash
python -m pip install -e .
```

You can confirm the installed package with:

```bash
python -m pip show benchpark-container-extension
```

## 7. Verify Benchpark plugin discovery

```bash
benchpark --help
benchpark container --help
benchpark cer --help
```

A correctly integrated installation exposes the external container and CER commands and the `+container` experiment feature.

## 8. Verify the native Spack path

After installing the extension, run at least one existing Benchpark experiment **without** `+container`.

The experiment must continue to use the existing Benchpark / Spack path.

For example, the analyzed result should still report values such as:

```text
EXPERIMENT_STATUS = SUCCESS
RAMBLE_STATUS     = SUCCESS
package_manager   = spack
```

The extension must not redirect ordinary experiments into the container execution path.

---

# Quick Start

The repository includes example Benchpark System, Experiment, and Ramble Application definitions under `examples/`.

## 1. Create a Container Catalog

The default Catalog registration file is:

```text
~/benchpark-containers/catalogs.yaml
```

A different visible configuration file can be selected with `BPCE_CONFIG`:

```bash
export BPCE_CONFIG="$HOME/benchpark-containers/catalogs.yaml"
```

Create and register a personal Catalog:

```bash
benchpark container catalog init \
  "$HOME/benchpark-containers/personal" \
  --name personal

benchpark container catalog add \
  "$HOME/benchpark-containers/personal" \
  --name personal
```

List registered Catalogs:

```bash
benchpark container catalog list
```

## 2. Register a SIF base environment

For an Apptainer or SingularityCE base image:

```bash
benchpark container register \
  --catalog personal \
  --name my-base \
  --release r1 \
  --kind sif \
  --source /absolute/path/to/base.sif \
  --sha256 <SIF_SHA256> \
  --platform linux/arm64 \
  --accelerator nvidia \
  --runtime apptainer \
  --runtime singularity \
  --python python3 \
  --shell bash
```

Inspect the registered release:

```bash
benchpark container list

benchpark container show \
  personal:my-base \
  --release r1

benchpark container validate \
  personal:my-base \
  --release r1
```

## 3. Register an OCI base environment

For Docker, register an OCI source:

```bash
benchpark container register \
  --catalog personal \
  --name my-docker-base \
  --release r1 \
  --kind oci \
  --source registry.example.org/team/base@sha256:<SOURCE_DIGEST> \
  --platform linux/arm64 \
  --accelerator nvidia \
  --runtime docker \
  --python python3 \
  --shell bash
```

OCI registration imports the image into the Managed Artifact Store. Later execution uses the retained managed artifact rather than re-resolving the source registry reference.

An explicit mutable tag may be used as **registration input**, but it is resolved and pinned during registration. Execution does not re-resolve that tag.

Do not rely on an implicit `latest`.

## 4. Register multiple runtime artifacts as one logical release

A single immutable Catalog release can contain different runtime-specific artifacts for the same logical base environment.

Create a manifest such as:

```yaml
schema_version: 1
name: pytorch-base
release: release-a
artifacts:
  - kind: sif
    uri: file:///absolute/path/to/pytorch-base.sif
    sha256: <SIF_SHA256>
    platform: linux/arm64
    accelerator: nvidia
    runtimes:
      - apptainer
      - singularity
    tools:
      python: python3
      shell: bash

  - kind: oci
    uri: registry.example.org/team/pytorch@sha256:<SOURCE_DIGEST>
    platform: linux/arm64
    accelerator: nvidia
    runtimes:
      - docker
    tools:
      python: python3
      shell: bash
```

Register it once:

```bash
benchpark container register \
  --catalog personal \
  --manifest pytorch-base.yaml
```

`name + release` is immutable. If the base environment changes, create a new release instead of replacing an existing one.

---

# Using the Bundled Examples

`tools/configure_examples.py` creates a separate Benchpark configuration scope for the examples shipped with this repository.

It does not require copying the example definitions into the upstream Benchpark tree.

```bash
python tools/configure_examples.py \
  ~/src/benchpark \
  /tmp/bpce-example-scope \
  --bootstrap /path/to/benchpark-bootstrap
```

Use the bootstrap directory associated with the Benchpark installation being tested.

## Initialize one System with multiple runtimes

Example for a Linux/ARM64 NVIDIA system:

```bash
benchpark -C /tmp/bpce-example-scope system init \
  --dest=/tmp/bpce-example/system \
  bpce-runtime \
  default_runtime=apptainer \
  container_platform=linux/arm64 \
  gpu_passthrough=nvidia \
  apptainer_executable="$(command -v apptainer)" \
  singularity_executable="$(command -v singularity || printf singularity)" \
  docker_executable="$(command -v docker)" \
  image_cache="$HOME/benchpark-containers/cache"
```

The System describes machine runtime capability. It does **not** contain the reusable image inventory.

## Initialize an Apptainer experiment

```bash
benchpark -C /tmp/bpce-example-scope experiment init \
  --dest=smoke-apptainer \
  /tmp/bpce-example/system \
  'common-base-smoke +container container_runtime=apptainer container_image=personal:pytorch-base container_release=release-a'
```

## Initialize a Docker experiment on the same System

```bash
benchpark -C /tmp/bpce-example-scope experiment init \
  --dest=smoke-docker \
  /tmp/bpce-example/system \
  'common-base-smoke +container container_runtime=docker container_image=personal:pytorch-base container_release=release-a'
```

The same System can therefore expose multiple container runtimes while each experiment instance selects the one it needs.

---

# Normal Benchpark / Ramble Workflow

Container support does not introduce a separate `benchpark container setup` execution path.

Use the normal Benchpark workflow.

For example:

```bash
benchpark -C /tmp/bpce-example-scope setup \
  /tmp/bpce-example/system/smoke-apptainer \
  /tmp/bpce-example/runs
```

Load the generated environment:

```bash
source /tmp/bpce-example/runs/setup.sh
```

Run the Ramble workspace:

```bash
WS=/tmp/bpce-example/runs/system/smoke-apptainer/workspace

ramble --workspace-dir "$WS" workspace setup
ramble --workspace-dir "$WS" on
ramble --workspace-dir "$WS" workspace analyze --formats json
```

The overall workflow remains:

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

Container use is selected through the Experiment specification, not through a separate setup command.

---

# Architecture

The extension separates machine capability, experiment intent, reusable container artifacts, runtime execution, and execution evidence.

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
      |     +-- platform / GPU passthrough
      |
      +-- Experiment
      |     +-- workload and parameters
      |     +-- image / release selection
      |     +-- run-local additions
      |
      +-- Container Catalog
      |     +-- logical name / release
      |     +-- Managed Artifact Store
      |
      +-- Runtime backend
      |     +-- Apptainer
      |     +-- SingularityCE
      |     +-- Docker
      |
      +-- CER
            +-- resolved state
            +-- observed state
            +-- attempt result
```

## System responsibility

A System describes what the machine can execute:

- available container runtimes
- runtime executables
- default runtime
- container platform
- GPU passthrough
- runtime cache and execution settings

A System does not own the reusable image inventory.

## Experiment responsibility

An Experiment describes benchmark intent:

- workload
- benchmark parameters
- logical container image and release
- requirements
- setup scripts
- mounted artifacts and environment

The Experiment definition is not tied to a single container runtime.

## Catalog responsibility

The Container Catalog owns reusable retained base environments:

- logical image name
- immutable release
- platform
- accelerator family
- runtime bindings
- managed SIF or OCI content
- source provenance

## How Benchpark discovers the extension

The Benchpark repository and this extension repository remain physically separate.

Running:

```bash
python -m pip install .
```

from the extension repository installs the extension package and its Python entry-point metadata into the Python environment used by Benchpark.

The generic plugin seam added to Benchpark Core discovers those entry points at runtime.

```text
~/src/
├── benchpark/
│     └── Benchpark Core
│
└── benchpark-container-extension/
      └── python -m pip install .
                   |
                   v
          Benchpark Python venv
                   |
                   | Python entry points
                   v
          Benchpark plugin seam
             /        |        \
       +container  container    cer
```

This separation is intentional:

```text
Source ownership:
    separate Git repositories

Runtime integration:
    shared Python environment
```

As a result, the container implementation does not need to be copied into the Benchpark source tree. Benchpark only contains the generic integration seam, while the extension package provides the external commands, lifecycle hooks, and `+container` feature through Python entry points.

---

# Container Catalog and Managed Artifacts

The Container Catalog is **not** only a list of links.

Registration imports container content into managed storage.

## SIF

A SIF is copied into content-addressed managed storage and verified by hash.

Conceptually:

```text
source.sif
   |
   | register
   v
Managed Artifact Store
   |
   +-- store/objects/sif/<sha256>/image.sif
```

After successful registration, the source file is provenance rather than an execution dependency.

## OCI

OCI images are imported into a filesystem OCI layout using `skopeo`.

Conceptually:

```text
registry reference
      |
      | register
      v
managed OCI layout
      |
      | restore when required
      v
Docker runtime
```

Execution can therefore restore the image from the managed filesystem rather than depending on a pre-existing Docker daemon image cache.

## Managed identity and source identity

For OCI images, a normalized managed representation can have an identity different from the source registry digest.

The model is:

```text
origin / source_digest  -> provenance
identity / stored_digest -> retained managed artifact identity
```

The extension verifies the managed artifact's own integrity and required blobs.

It does not claim semantic equivalence between a source representation and a normalized managed OCI representation.

## Immutable releases

`name + release` is immutable.

For example:

```text
pytorch-base / release-a
pytorch-base / release-b
```

If the base environment changes, register a new release.

## Catalog removal

Removing a Catalog alias unregisters it from lookup.

It does not automatically delete managed image content or the Catalog directory.

Automatic garbage collection is not part of the current implementation.

---

# Runtime Selection

A System may expose multiple runtime capabilities:

```text
available:
  - singularity
  - apptainer
  - docker

default:
  singularity
```

An experiment may override the System default:

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

If the selected runtime is unavailable, has the wrong identity, or cannot use the selected artifact, execution fails explicitly.

The extension does **not** silently fall back to a different runtime.

An Apptainer executable or symlink named `singularity` is not treated as a genuine SingularityCE runtime.

---

# Run-local Environment

Managed base containers are treated as immutable reusable bases.

Experiment-specific additions are prepared separately for each run, including:

```text
/bpce/python
/bpce/tools
```

The container's own Python, pip, and shell are used.

The extension does not inject an unrelated host Python environment into the container.

Typical additions include:

- pinned Python requirements
- experiment setup scripts
- benchmark-specific helper tools

The managed base artifact remains unchanged.

If an addition becomes stable and reusable across experiments, create a new base image and Catalog release.

---

# Container Execution Record (CER)

A CER records a concrete container execution attempt.

Recorded information can include:

- run and attempt identity
- resolved runtime selection
- runtime version and observed runtime state
- managed image identity
- Catalog name and release
- observed image information
- GPU information
- run-local additions
- execution status
- failure phase
- result information

Failed attempts are retained rather than overwritten.

## List CERs

```bash
benchpark cer list /path/to/workspace
```

## Show a CER

```bash
benchpark cer show /path/to/cer.json
```

## Validate a CER structure

```bash
benchpark cer validate /path/to/cer.json
```

## Compare two CERs

```bash
benchpark cer diff \
  /path/to/left/cer.json \
  /path/to/right/cer.json
```

## Export an existing CER

```bash
benchpark cer export \
  /path/to/cer.json \
  --output /path/to/exported-cer.json
```

CER structural validation does not claim scientific equivalence between benchmark runs.

---

# Reproducibility Model

Within its managed scope, the extension is designed to preserve the following properties:

- existing `name + release` Catalog entries are immutable
- execution uses the retained Managed Artifact rather than re-resolving the source registry tag
- managed SIF content remains usable independently of the original source path
- managed OCI content can be restored independently of a pre-existing Docker daemon image
- runtime selection is explicit and recorded
- selected image identity and Catalog release are recorded
- failed and successful attempts can coexist in CER history
- run-local additions do not modify the managed base artifact
- experiments without `+container` continue to use the native Benchpark path

The extension does **not** by itself guarantee:

- identical performance across different hardware
- semantic equivalence between a source OCI representation and its normalized managed representation
- permanent availability of external package repositories
- licensing rights for third-party images or packages
- registry authentication
- container signature verification
- backup of the Managed Artifact Store
- protection from storage-device failure or administrator deletion
- site-specific GPU or scheduler configuration
- scientific equivalence between two benchmark runs

Storage capacity, backup, retention, authentication, licensing, and site security remain operational responsibilities.

---

# Validation Status

The current implementation has been exercised end-to-end on an NVIDIA DGX Spark environment.

Validation platform:

```text
Architecture: aarch64
GPU:          NVIDIA GB10
CUDA:         13.0
Apptainer:    1.5.0
Docker:       28.5.1
Benchpark:    RIKEN-RCCS/benchpark FN_apps
Commit:       817a8a9b29ce3a9c5cca39d8a53c97b6ae6ca0f7
```

Validated flows:

| Validation item | Status |
|---|---:|
| Existing Benchpark plus generated Core integration patch | PASS |
| Extension discovery through Benchpark CLI | PASS |
| Managed SIF registration and validation | PASS |
| Benchpark -> Ramble -> Managed SIF -> Apptainer | PASS |
| Apptainer NVIDIA GPU execution | PASS |
| Managed OCI registration and validation | PASS |
| Benchpark -> Ramble -> Managed OCI -> Docker | PASS |
| Docker NVIDIA GPU execution | PASS |
| Per-experiment Apptainer / Docker selection on one System | PASS |
| Run-local requirements and setup additions | PASS |
| CER creation and structural validation | PASS |
| Managed SIF execution after removing the registration source copy | PASS |
| Managed OCI restoration after removing the Docker daemon image | PASS |
| Native Benchpark / Spack non-regression | PASS |
| BabelStream 5.0 CUDA execution on NVIDIA GB10 | PASS |
| Apptainer masquerading as SingularityCE is rejected | PASS |
| Genuine SingularityCE end-to-end validation | PENDING |

The SingularityCE backend is implemented. Final acceptance on a genuine SingularityCE installation remains pending.

---

# Repository Layout

The public repository contains only source, packaging, examples, tests, and integration tooling:

```text
benchpark-container-extension/
├── core/
│   └── generic Benchpark Core integration seam
├── examples/
│   └── example Systems, Experiments, Applications, and validation inputs
├── LICENSE
├── MANIFEST.in
├── NOTICE
├── pyproject.toml
├── README.md
├── src/
│   └── installable extension packages and runtime resources
├── tests/
│   └── automated tests
└── tools/
    └── patch generation, example configuration, packaging, and validation tools
```

Historical source snapshots, internal design notes, review reports, generated test results, and build artifacts are not part of the public repository.

Release history should be maintained through Git tags rather than by embedding previous source trees in the current repository.

---

# Development

For development against a Benchpark checkout, activate the same Python virtual environment used by Benchpark before installing development dependencies:

```bash
source /path/to/benchpark-venv/bin/activate
python -m pip install -e '.[test]'
```

Run the test suite:

```bash
python -m pytest -q
```

Verify packaged runtime resources:

```bash
python tools/build_runtime.py --check
```

Run the architecture audit:

```bash
python tools/audit_architecture.py
```

When testing against a Benchpark checkout, generate a fresh integration patch for that checkout:

```bash
python tools/patch_core.py \
  /path/to/benchpark \
  --output /tmp/bpce-core.patch

git -C /path/to/benchpark apply --stat /tmp/bpce-core.patch
git -C /path/to/benchpark apply --check /tmp/bpce-core.patch
```

Do not edit generated Benchpark or Ramble workspace files to change experiment intent. Change the System or Experiment source definition and initialize a new instance.

---

# License

Licensed under the Apache License, Version 2.0.

See `LICENSE` and `NOTICE`.

---

# Acknowledgments

This work is based on results obtained from the project, “Research and Development Project of the Enhanced Infrastructures for Post-5G Information and Communication Systems” (JPNP25013), commissioned by the New Energy and Industrial Technology Development Organization (NEDO).
