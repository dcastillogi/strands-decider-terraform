#!/usr/bin/env python3
"""Read-only resource telemetry on the decider instance: CPU, memory, GPU utilisation, VRAM and power."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def cpus():
    result = {}
    for line in Path('/proc/stat').read_text().splitlines():
        fields = line.split()
        if fields[0].startswith('cpu'):
            values = list(map(int, fields[1:9]))
            result[fields[0]] = (sum(values), values[3] + values[4], values[4], values[7])
    return result

def ticks(path):
    # Ignore the parenthesized command name (which can contain spaces).
    values = Path(path).read_text().rsplit(')', 1)[1].split()
    return int(values[11]) + int(values[12])

def process_tree(root_pid):
    parents = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            parents[path.parent.name] = path.read_text().rsplit(')', 1)[1].split()[1]
        except (FileNotFoundError, ProcessLookupError):
            pass
    selected = {root_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in selected}
        if children <= selected:
            return selected
        selected |= children

def sample_processes(root_pid):
    processes, threads = {}, {}
    for child in process_tree(root_pid):
        try:
            processes[child] = ticks(f'/proc/{child}/stat')
            for task in Path(f'/proc/{child}/task').iterdir():
                try:
                    threads[f'{child}/{task.name}'] = ticks(task / 'stat')
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return processes, threads

pid = subprocess.check_output(['systemctl', 'show', '-p', 'MainPID', '--value', 'strands-decider'], text=True).strip()
hz = os.sysconf('SC_CLK_TCK')
previous = cpus()
previous_proc, previous_threads = sample_processes(pid)
previous_time = time.monotonic()
with open(sys.argv[1], 'w', buffering=1) as output:
    for _ in range(int(sys.argv[2])):
        time.sleep(1)
        now = time.monotonic()
        current = cpus()
        current_proc, threads = sample_processes(pid)
        elapsed = now - previous_time
        cpu = {}
        for name, vals in current.items():
            delta = vals[0] - previous[name][0]
            cpu[name] = round(100 * (1 - (vals[1] - previous[name][1]) / max(delta, 1)), 1)
        aggregate_delta = max(current['cpu'][0] - previous['cpu'][0], 1)
        memory = {a: int(b.split()[0]) for a, b in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
        gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,power.draw', '--format=csv,noheader,nounits'], text=True).strip().split(', ')
        top_threads = sorted((100 * (value - previous_threads.get(tid, value)) / hz / elapsed for tid, value in threads.items()), reverse=True)[:5]
        output.write(json.dumps({'epoch': time.time(), 'cpu_busy_pct': cpu,
                     'cpu_iowait_pct': round(100 * (current['cpu'][2] - previous['cpu'][2]) / aggregate_delta, 1),
                     'cpu_steal_pct': round(100 * (current['cpu'][3] - previous['cpu'][3]) / aggregate_delta, 1),
                     'process_cpu_pct': round(100 * sum(value - previous_proc.get(child, value)
                        for child, value in current_proc.items()) / hz / elapsed, 1),
                     'top_thread_cpu_pct': [round(v, 1) for v in top_threads],
                     'processes': len(current_proc), 'threads': len(threads), 'ram_available_mib': round(memory['MemAvailable'] / 1024),
                     'gpu_util_pct': float(gpu[0]), 'vram_mib': float(gpu[1]), 'gpu_power_w': float(gpu[2])}) + '\n')
        previous, previous_proc, previous_threads, previous_time = current, current_proc, threads, now
