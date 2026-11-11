# SPDX-License-Identifier: Apache-2.0
"""Shared native-SIF behavior. Apptainer and Singularity remain distinct backends."""
import csv
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .base import RuntimeBackend, RuntimeCapabilities, ImageRef, oci_reference
from ..util import ValidationError, identity, atomic_json, sha256


class SIFRuntimeBackend(RuntimeBackend):
    private_image_cache = True
    capabilities = RuntimeCapabilities(readonly_overlay_formats=frozenset({"squashfs"}),
                                       image_build_formats=frozenset({"sif-definition-v1"}))
    env_prefix = ""
    cache_namespace = ""

    @classmethod
    def validate_config(cls, settings):
        from ..util import strict
        options = settings.get('backend_options', {})
        strict(options, ('isolation', 'site_mounts'), 'SIF site options')
        if options.get('isolation', 'contained') not in ('contained', 'host-ipc-pid'):
            raise ValidationError('Unknown SIF isolation policy')
        if not isinstance(options.get('site_mounts', []), list):
            raise ValidationError('site_mounts must be a list')
        for entry in options.get('site_mounts', []):
            strict(entry, ('source','target','readonly'), 'site mount', ('source','target','readonly'))
            from .base import Mount
            from ..contracts import validate_targets
            Mount(entry['source'], entry['target'], entry['readonly'])
            validate_targets([{'target':entry['target']}], concrete=True)

    def host_env(self):
        env = super().host_env()
        env[self.env_prefix + "CACHEDIR"] = str(self.image_cache / "oci-cache")
        return env

    @property
    def image_cache(self):
        return self.cache / self.cache_namespace if self.cache_namespace else self.cache

    def image_build_command(self, output, definition, *, fakeroot):
        if type(fakeroot) is not bool:
            raise ValidationError("fakeroot must be explicitly declared")
        # Never use sudo, --force, --notest, or host %setup scripts.
        return [self.executable, "build"] + (["--fakeroot"] if fakeroot else []) + [str(output), str(definition)]

    def check_built_image(self, path, platform, log, timeout):
        # Setup already requires a build node matching the declared CPU platform.
        # Validate SIF structure and container startup; uname would report the
        # shared host kernel, NOT independently prove rootfs/GPU compatibility.
        from ..image_build import run_logged
        run_logged([self.executable, "sif", "list", str(path)], log, timeout, self.host_env())
        run_logged([self.executable, "exec", "--cleanenv", str(path), "/bin/sh", "-c", ":"],
                   Path(str(log) + ".startup"), timeout, self.host_env())

    def resolve_image(self, base, timeout):
        from ..environment_snapshot import validate_environment_binding
        snapshot = validate_environment_binding(base)
        image = self._resolve_base_image(base, timeout)
        if snapshot is None:
            return image
        details = dict(image.details, environment_snapshot=snapshot,
                       environment_sha256=snapshot["environment_sha256"])
        if "overlay" in base:
            details["overlay"] = base["overlay"]
        return ImageRef(image.kind, image.reference, image.identity, details)

    def _resolve_base_image(self, base, timeout):
        if isinstance(base.get("managed"), dict):
            from ..image_store import verify_managed
            verified = verify_managed(base)
            if base.get("kind") == "oci":
                return self._managed_oci(base, verified, timeout)
        if base.get("kind") == "sif":
            path = Path(base["source"])
            if not path.is_file() or sha256(path) != base["sif_sha256"]:
                raise ValidationError("Pinned local SIF has changed")
            return self._reference(path)
        uri = oci_reference(base)
        cache = self.image_cache
        cache.mkdir(parents=True, exist_ok=True)
        # Preserve the original Apptainer cache key and verification metadata.
        key = identity({"uri": base["uri"], "platform": base.get("platform")})
        sif, metadata = cache / (key + ".sif"), cache / (key + ".json")
        with open(cache / (key + ".lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if sif.exists():
                if not metadata.is_file():
                    raise ValidationError("Existing image has no verification metadata")
                saved = json.loads(metadata.read_text())
                if saved["uri"] != base["uri"] or saved["sif_sha256"] != sha256(sif):
                    raise ValidationError("Cached SIF has changed")
                return self._reference(sif)
            fd, temporary = tempfile.mkstemp(prefix=".pull-", suffix=".sif", dir=cache)
            os.close(fd)
            os.unlink(temporary)
            try:
                subprocess.run([self.executable, "pull", temporary, "docker://" + uri],
                               check=True, env=self.host_env(), timeout=timeout)
                digest = sha256(temporary)
                os.rename(temporary, sif)
                atomic_json(metadata, {"uri": base["uri"], "sif_sha256": digest})
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return self._reference(sif)

    def _managed_oci(self, base, verified, timeout):
        # Conversion uses retained local bytes, not the original registry.
        # The derived SIF cache is not the authoritative store; its exact hash
        # and the original OCI identity are both observed in CER.
        cache = self.image_cache
        cache.mkdir(parents=True, exist_ok=True)
        version = self.version()
        descriptor = {"stored_digest": verified["root_digest"],
                      "manifest_digest": verified["manifest_digest"],
                      "platform": base["platform"], "runtime": self.name, "version": version}
        key = identity(descriptor)
        sif, metadata = cache / (key + ".sif"), cache / (key + ".json")
        with open(cache / (key + ".lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if sif.exists():
                if not metadata.is_file():
                    raise ValidationError("Cached managed-OCI SIF has no verification metadata")
                saved = json.loads(metadata.read_text())
                if saved.get("source") != descriptor or saved.get("sif_sha256") != sha256(sif):
                    raise ValidationError("Cached managed-OCI SIF identity changed")
            else:
                fd, temporary = tempfile.mkstemp(prefix=".oci-build-", suffix=".sif", dir=cache)
                os.close(fd); os.unlink(temporary)
                try:
                    from ..image_store import selected_layout
                    with tempfile.TemporaryDirectory(prefix="oci-view-", dir=cache) as view_root:
                        view = selected_layout(verified, Path(view_root) / "layout")
                        subprocess.run([self.executable, "build", temporary, "oci:" + str(view)],
                                       check=True, timeout=timeout, env=self.host_env())
                    digest = sha256(temporary)
                    os.rename(temporary, sif)
                    atomic_json(metadata, {"source": descriptor, "sif_sha256": digest})
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
        image = self._reference(sif)
        return ImageRef(image.kind, image.reference, image.identity,
                        dict(image.details, managed_source=True, source_oci_digest=verified["root_digest"],
                             source_manifest_digest=verified["manifest_digest"], conversion=descriptor))

    def resolve_local_image(self, base, timeout):
        if base.get("kind") == "oci" and not isinstance(base.get("managed"), dict):
            key = identity({"uri": base["uri"], "platform": base.get("platform")})
            if not (self.image_cache / (key + ".sif")).is_file():
                raise ValidationError("OFFLINE_IMAGE_MISSING: run online workspace setup first")
        # Managed OCI conversion uses retained local blobs, not the network.
        return self.resolve_image(base, timeout)

    def load_prepared_image(self, base, prepared, timeout):
        if prepared.get("kind") != "sif":
            raise ValidationError("Prepared SIF image is missing")
        image = self._reference(prepared["reference"])
        if image.identity != prepared["identity"]:
            raise ValidationError("Prepared SIF content changed")
        if base.get("kind") == "sif" and image.details["sif_sha256"] != base["sif_sha256"]:
            raise ValidationError("Prepared SIF differs from requested Base")
        details = prepared.get("details", {})
        if details.get("sif_sha256") != image.details["sif_sha256"]:
            raise ValidationError("Prepared image observation mismatch")
        if base.get("environment_snapshot") is not None:
            from ..environment_snapshot import verify_environment_files
            verify_environment_files(base)
            if details.get("environment_snapshot") != base["environment_snapshot"]:
                raise ValidationError("Prepared composition differs from requested Base")
        return ImageRef(image.kind, image.reference, image.identity, details)

    def _reference(self, path):
        digest = sha256(path)
        return ImageRef("sif", str(path), "sha256:" + digest,
                        {"sif_path": str(path), "sif_sha256": digest})

    def image(self, base, timeout):
        """Retain the earlier helper's path-returning interface."""
        return Path(self.resolve_image(base, timeout).reference)

    def observe_image(self, image):
        if not isinstance(image, ImageRef):
            image = self._reference(image)
        return super().observe_image(image)

    def build_command(self, request):
        self.validate_request(request)
        self.execution_binding(request)
        image = request.image
        if isinstance(image, ImageRef) and image.kind != "sif":
            raise ValidationError("Resolve OCI to a pinned SIF before building the command")
        if any(c in str(image) for c in ':,\n\r\x00') or str(image).startswith('-'):
            raise ValidationError("Cannot encode bind/image path: " + str(image))
        scratch_mount = next(
            (m for m in request.mounts if m.target == "/bpce" and not m.readonly),
            None,
        )
        if scratch_mount is None:
            raise ValidationError(
                "SIF contained execution requires the writable /bpce scratch mount"
            )
        contained_workdir = Path(scratch_mount.source) / ".contained-workdir"
        contained_workdir.mkdir(parents=True, exist_ok=True)

        args = [
            self.executable,
            "exec",
            "--cleanenv",
            "--containall" if self.settings.get("backend_options", {}).get("isolation", "contained") == "contained" else "--contain",
            "--no-eval",
            "--workdir",
            str(contained_workdir),
        ]
        if request.accelerator == "nvidia":
            args.append("--nv")
        elif request.accelerator == "amd":
            args.append("--rocm")
        elif request.accelerator != "none":
            raise ValidationError("Unknown GPU passthrough setting")
        overlay = image.details.get("overlay") if isinstance(image, ImageRef) else None
        if overlay is not None:
            from ..environment_snapshot import verify_overlay, validate_snapshot
            snapshot = image.details.get("environment_snapshot")
            validate_snapshot(snapshot)
            comp = snapshot["composition"]
            if comp["base"] != {"kind": "sif", "digest": "sha256:" + image.details["sif_sha256"]} or comp["overlay"] != {k: overlay[k] for k in ("format", "sha256", "readonly")}:
                raise ValidationError("Overlay request does not match its frozen environment")
            verify_overlay(overlay)
            path = overlay["source"]
            if not isinstance(path, str) or not Path(path).is_absolute() or any(c in path for c in ":,\n\r\x00"):
                raise ValidationError("Cannot encode Overlay path")
            args += ["--overlay", path + ":ro"]
        for mount in request.mounts:
            args += ["--bind", mount.source + ":" + mount.target + (":ro" if mount.readonly else ":rw")]
        args += ["--pwd", request.workdir]
        for key, value in sorted(request.environment.items()):
            # --env is a CSV stringToString flag. Quote comma-containing values
            # rather than rejecting multi-GPU visibility such as 0,2.
            stream = io.StringIO()
            csv.writer(stream, lineterminator="").writerow([key + "=" + value])
            args += ["--env", stream.getvalue()]
        return args + [str(image)] + list(request.command)
