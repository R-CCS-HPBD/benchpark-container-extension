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

from .base import RuntimeBackend, ImageRef, oci_reference
from ..util import ValidationError, identity, atomic_json, sha256


class SIFRuntimeBackend(RuntimeBackend):
    env_prefix = ""
    cache_namespace = ""

    def host_env(self):
        env = super().host_env()
        env[self.env_prefix + "CACHEDIR"] = str(self.image_cache / "oci-cache")
        return env

    @property
    def image_cache(self):
        return self.cache / self.cache_namespace if self.cache_namespace else self.cache

    def resolve_image(self, base, timeout):
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
        image = request.image
        if isinstance(image, ImageRef) and image.kind != "sif":
            raise ValidationError("Resolve OCI to a pinned SIF before building the command")
        if any(c in str(image) for c in ':,\n\r\x00') or str(image).startswith('-'):
            raise ValidationError("Cannot encode bind/image path: " + str(image))
        args = [self.executable, "exec", "--cleanenv", "--containall", "--no-home", "--no-eval"]
        if request.accelerator == "nvidia":
            args.append("--nv")
        elif request.accelerator == "amd":
            args.append("--rocm")
        elif request.accelerator != "none":
            raise ValidationError("Unknown GPU passthrough setting")
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
