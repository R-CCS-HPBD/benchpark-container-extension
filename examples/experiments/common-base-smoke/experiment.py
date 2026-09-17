# SPDX-License-Identifier: Apache-2.0
"""Small functional example, not an LLM performance benchmark.

Reuses Base torch, mounts a requirements declaration and Git-managed setup script,
executes matrixed sizes and records per-attempt provenance. No image/runtime
URI belongs in this experiment. No Container mixin is required.
"""
from benchpark.directives import variant
from benchpark.experiment import Experiment
from benchpark.programming_model import ProgrammingModel, ProgrammingModelType


class CommonBaseSmoke(Experiment, ProgrammingModel(ProgrammingModelType.Mpionly)):
    # The user selects +container; native package-manager defaults are untouched.

    variant('workload',default='smoke',values=('smoke',),description='Fixture workload')
    variant('model',default='linear',values=('linear','mlp'),multi=True,description='Toy model; not a downloaded LLM')
    variant('size',default='16',values=int,multi=True,description='Matrix dimension; Base=16')
    variant('steps',default='5',values=int,description='Timed iterations; Base=5')
    variant('warmup',default='1',values=int,description='Untimed warmup iterations')
    variant('execution_mode',default='no-grad',values=('no-grad','inference'),description='Base execution context; does not change workload')
    variant('tune',default='none',description='Fixed preset name under tuning/')
    extension_request_settings={'tune':{'allowed':{'execution_mode':{'choices':['no-grad','inference']}}}}

    def get_extension_settings(self):
        return {'container':{'schema_version':2,'default_image':'pytorch-base','default_release':'release-a',
            'requirements':['requirements/extra.txt'],
            'setup_scripts':['setup/install-demo-tool.sh'],
            'protected_packages':{'torch':'torch'},'smoke_imports':[],
            'executables':['benchmark'],
            'variables':['model','size','steps','warmup','execution_mode'],
            'artifacts':[
                {'name':'script','kind':'script','source':'assets/benchmark.py','target':'/bench/benchmark.py'},
                {'name':'workload','kind':'workload','source':'workloads/smoke.json','target':'/bench/workload.json'},
                {'name':'result','kind':'result','target':'/results'}],
            'environment':{'OMP_NUM_THREADS':'1'},'timeout_seconds':600}}

    def compute_applications_section(self):
        # This is the associated Ramble application's repository name.
        self.name='common-base-smoke'
        for name in ('model','size'):
            values=list(self.spec.variants[name])
            self.add_experiment_variable(name,values,named=True,matrixed=True)
        for name in ('steps','warmup','execution_mode'):
            self.add_experiment_variable(name,self.spec.variants[name][0],named=False)
        self.add_experiment_variable('n_nodes',1,named=False)
        self.add_experiment_variable('n_ranks',1,named=False)
        self.add_experiment_variable('processes_per_node',1,named=False)
        self.set_required_variables(n_resources='1',process_problem_size='{size}',total_problem_size='{size}')

    def compute_package_section(self):
        # Used only by the native user-managed counterpart. The selected
        # Container provider skips this application's package generation.
        self.add_package_spec(self.name,[self.name])
