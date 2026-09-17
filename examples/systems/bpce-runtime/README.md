# Runtime-neutral development System

This is a one-process example, not a production scheduler template. Declare the selected runtime executable, immutable Base URI, image Python/Bash and optional System backend-options file at system initialization. `container_runtime` selects the registry backend; `gpu_passthrough` keeps the existing none/nvidia/amd values.

The unchanged common-base-smoke Experiment uses logical Base `pytorch-base`. Its smoke is CPU functional validation and does not prove GPU compute or nonempty dependency installation. Use the real-runtime runbook for those acceptance gates. Real Benchpark execution of this new System is PENDING-EXTERNAL.

See `docs/RUNTIME_VALIDATION.ja.md` and `docs/RUNTIME_BACKENDS.ja.md`. Do not edit an already initialized System/Experiment/workspace to change runtime; create another fixed experiment instead.
