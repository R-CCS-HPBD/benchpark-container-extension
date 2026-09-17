# SPDX-License-Identifier: Apache-2.0
from ramble.appkit import *


class CommonBaseSmoke(ExecutableApplication):
    name='common-base-smoke'
    tags('functional-test')
    executable('benchmark',
        '{bpce_python} /bench/benchmark.py --model {model} --size {size} --steps {steps} --warmup {warmup} --execution-mode {execution_mode}',
        use_mpi=False)
    workload('smoke',executables=['benchmark'])
    for variable,value in [('model','linear'),('size','16'),('steps','5'),('warmup','1'),('execution_mode','no-grad')]:
        workload_variable(variable,default=value,description='Functional fixture input',workloads=['smoke'])
    success_criteria('completed',mode='string',match=r'BPCE_SMOKE_OK',file='{log_file}')
    figure_of_merit('toy_rate',log_file='{log_file}',fom_regex=r'BPCE_TOY_RATE=(?P<rate>[0-9.]+)',group_name='rate',units='iterations/s')
