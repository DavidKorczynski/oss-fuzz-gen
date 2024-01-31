#!/usr/bin/env python3
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run an experiment with all function-under-tests."""

import argparse
import logging
import os
import sys
import traceback
from multiprocessing import Pool

import run_one_experiment
from experiment import benchmark as benchmarklib
from experiment.workdir import WorkDirs
from llm_toolkit import models

# WARN: Avoid large NUM_EXP for local experiments.
# NUM_EXP controls the number of experiments in parallel, while each experiment
# will evaluate {run_one_experiment.NUM_EVA, default 3} fuzz targets in
# parallel.
NUM_EXP = int(os.getenv('LLM_NUM_EXP', '2'))

# Default LLM hyper-parameters.
MAX_TOKENS: int = run_one_experiment.MAX_TOKENS
NUM_SAMPLES: int = run_one_experiment.NUM_SAMPLES
RUN_TIMEOUT: int = run_one_experiment.RUN_TIMEOUT
TEMPERATURE: float = run_one_experiment.TEMPERATURE

BENCHMARK_DIR: str = './benchmark-sets/comparison'
RESULTS_DIR: str = run_one_experiment.RESULTS_DIR


class Result:
  benchmark: benchmarklib.Benchmark
  result: run_one_experiment.AggregatedResult | str

  def __init__(self, benchmark, result):
    self.benchmark = benchmark
    self.result = result


def get_experiment_configs(
    args: argparse.Namespace
) -> list[tuple[benchmarklib.Benchmark, argparse.Namespace]]:
  """Constructs a list of experiment configs based on the |BENCHMARK_DIR| and
    |args| setting."""
  benchmark_yamls = []
  if args.benchmark_yaml:
    print(f'A benchmark yaml file ({args.benchmark_yaml}) is provided. '
          f'Will use it and ignore the files in {args.benchmarks_directory}.')
    benchmark_yamls = [args.benchmark_yaml]
  else:
    benchmark_yamls = [
        os.path.join(args.benchmarks_directory, file)
        for file in os.listdir(args.benchmarks_directory)
        if file.endswith('.yaml') or file.endswith('yml')
    ]
  experiment_configs = []
  for benchmark_file in benchmark_yamls:
    experiment_configs.extend(benchmarklib.Benchmark.from_yaml(benchmark_file))

  return [(config, args) for config in experiment_configs]


def run_experiments(benchmark: benchmarklib.Benchmark,
                    args: argparse.Namespace) -> Result:
  """Runs an experiment based on the |benchmark| config."""
  try:
    work_dirs = WorkDirs(os.path.join(args.work_dir, f'output-{benchmark.id}'))
    model = models.LLM.setup(
        ai_binary=args.ai_binary,
        prompt_path=work_dirs.prompt,
        name=args.model,
        template_dir=args.template_directory,
        max_tokens=MAX_TOKENS,
        num_samples=args.num_samples,
        temperature=args.temperature,
    )

    result = run_one_experiment.run(
        benchmark=benchmark,
        model=model,
        work_dirs=work_dirs,
        cloud_experiment_name=args.cloud_experiment_name,
        cloud_experiment_bucket=args.cloud_experiment_bucket,
        use_context=args.context,
        run_timeout=args.run_timeout)
    return Result(benchmark, result)
  except Exception as e:
    print('Exception while running experiment:', e, file=sys.stderr)
    traceback.print_exc()
    return Result(benchmark, f'Exception while running experiment: {e}')


def parse_args() -> argparse.Namespace:
  """Parses command line arguments."""
  parser = argparse.ArgumentParser(
      description='Run all experiments that evaluates all target functions.')
  parser.add_argument('-n',
                      '--num-samples',
                      type=int,
                      default=NUM_SAMPLES,
                      help='The number of samples to request from LLM.')
  parser.add_argument(
      '-t',
      '--temperature',
      type=float,
      default=TEMPERATURE,
      help=('A value between 0 and 1 representing the variety of the targets '
            'generated by LLM.'))
  parser.add_argument('-c',
                      '--cloud-experiment-name',
                      type=str,
                      default='',
                      help='The name of the cloud experiment')
  parser.add_argument('-cb',
                      '--cloud-experiment-bucket',
                      type=str,
                      default='',
                      help='A gcloud bucket to store experiment files.')
  parser.add_argument('-b', '--benchmarks-directory', type=str)
  parser.add_argument('-y',
                      '--benchmark-yaml',
                      type=str,
                      help='A benchmark YAML file')
  parser.add_argument('-to', '--run-timeout', type=int, default=RUN_TIMEOUT)
  parser.add_argument('-a',
                      '--ai-binary',
                      required=False,
                      nargs='?',
                      const=os.getenv('AI_BINARY', ''),
                      default='',
                      type=str)
  parser.add_argument('-l',
                      '--model',
                      default=models.DefaultModel.name,
                      help=('Models available: '
                            f'{", ".join(models.LLM.all_llm_names())}'))
  parser.add_argument('-td',
                      '--template-directory',
                      type=str,
                      default=models.DEFAULT_TEMPLATE_DIR)
  parser.add_argument('-w', '--work-dir', default=RESULTS_DIR)
  parser.add_argument('--context',
                      action='store_true',
                      default=False,
                      help='Add context to function under test.')

  args = parser.parse_args()
  if args.num_samples:
    assert args.num_samples > 0, '--num-samples must take a positive integer.'

  if args.temperature:
    assert 2 >= args.temperature >= 0, '--temperature must be within 0 and 2.'

  benchmark_yaml = args.benchmark_yaml
  if benchmark_yaml:
    assert (benchmark_yaml.endswith('.yaml') or
            benchmark_yaml.endswith('yml')), (
                "--benchmark-yaml needs to take an YAML file.")

  assert bool(benchmark_yaml) != bool(args.benchmarks_directory), (
      'One and only one of --benchmark-yaml and --benchmarks-directory is '
      'required. The former takes one benchmark YAML file, the latter '
      'takes: a directory of them.')

  # Validate templates.
  assert os.path.isdir(args.template_directory), (
      '--template-directory must be an existing directory.')

  # Validate cloud experiment configs.
  assert (
      bool(args.cloud_experiment_name) == bool(args.cloud_experiment_bucket)
  ), ('Cannot accept exactly one of --args.cloud-experiment-name and '
      '--args.cloud-experiment-bucket: Local experiment requires neither of '
      'them, cloud experiment needs both.')
  return args


def _print_experiment_result(result: Result):
  """Prints the |result| of a single experiment."""
  print(f'\n**** Finished benchmark {result.benchmark.project}, '
        f'{result.benchmark.function_name} ****\n'
        f'{result.result}')


def _print_experiment_results(results: list[Result]):
  """Prints the |results| of multiple experiments."""
  print('\n\n**** FINAL RESULTS: ****\n\n')
  for result in results:
    print('=' * 80)
    print(f'*{result.benchmark.project}, {result.benchmark.function_name}*\n'
          f'{result.result}\n')


def main():
  logging.basicConfig(level=logging.INFO)
  args = parse_args()
  run_one_experiment.prepare()

  experiment_configs = get_experiment_configs(args)
  experiment_results = []

  print(f'Running {NUM_EXP} experiment(s) in parallel.')
  if NUM_EXP == 1:
    for config in experiment_configs:
      result = run_experiments(*config)
      experiment_results.append(result)
      _print_experiment_result(result)
  else:
    with Pool(NUM_EXP) as p:
      for result in p.starmap(run_experiments, experiment_configs):
        experiment_results.append(result)
        _print_experiment_result(result)

  _print_experiment_results(experiment_results)


if __name__ == '__main__':
  sys.exit(main())
