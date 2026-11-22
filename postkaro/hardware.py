"""Hardware monitoring and agent status for PostKaro.

Inspects local CPU, NVIDIA GPU (CUDA + NVENC), RAM, and Storage,
and reports agent connection and execution status.
"""
import os
import platform
import subprocess
import psutil

# Cache static hardware info
_STATIC_CPU = None
_STATIC_GPU = None


def get_cpu_info():
    global _STATIC_CPU
    usage = psutil.cpu_percent(interval=0.1)
    cores = psutil.cpu_count(logical=True)
    physical_cores = psutil.cpu_count(logical=False) or cores

    if not _STATIC_CPU:
        name = platform.processor() or 'CPU'
        if os.name == 'nt':
            try:
                r = subprocess.run(['wmic', 'cpu', 'get', 'name'], capture_output=True, text=True, timeout=2)
                lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and 'Name' not in l]
                if lines:
                    name = lines[0]
            except Exception:
                pass
        _STATIC_CPU = name

    return {
        'name': _STATIC_CPU,
        'cores': cores,
        'physical_cores': physical_cores,
        'usage_percent': round(usage, 1)
    }


def get_gpu_info():
    global _STATIC_GPU
    gpu = {
        'name': 'No dedicated GPU detected',
        'available': False,
        'vram_total_mb': 0,
        'vram_used_mb': 0,
        'vram_percent': 0,
        'nvenc': False,
        'cuda': False
    }

    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,memory.used', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().split('\n')[0].split(',')]
            total = int(parts[1])
            used = int(parts[2])
            pct = round((used / total) * 100, 1) if total else 0
            gpu.update({
                'name': parts[0],
                'available': True,
                'vram_total_mb': total,
                'vram_used_mb': used,
                'vram_percent': pct,
                'nvenc': True,
                'cuda': True
            })
    except Exception:
        pass

    return gpu


def get_ram_info():
    m = psutil.virtual_memory()
    return {
        'total_gb': round(m.total / (1024**3), 1),
        'used_gb': round(m.used / (1024**3), 1),
        'free_gb': round(m.available / (1024**3), 1),
        'percent': round(m.percent, 1)
    }


def get_storage_info():
    target = 'D:\\' if os.path.exists('D:\\') else '.'
    d = psutil.disk_usage(target)
    return {
        'drive': target,
        'total_gb': round(d.total / (1024**3), 1),
        'free_gb': round(d.free / (1024**3), 1),
        'used_gb': round(d.used / (1024**3), 1),
        'percent': round(d.percent, 1)
    }


def get_telemetry(active_task=None):
    """
    Returns full hardware status, agent status, and active task progress.
    Agent status: 'Processing' if a task is active, 'Idle' if queue empty, 'Offline' if agent not responding.
    """
    cpu = get_cpu_info()
    gpu = get_gpu_info()
    ram = get_ram_info()
    storage = get_storage_info()

    if active_task and active_task.get('stage') in ('downloading', 'transcribing', 'picking_moments', 'rendering'):
        agent_status = 'Processing'
    else:
        agent_status = 'Idle'

    return {
        'agent_status': agent_status,
        'cpu': cpu,
        'gpu': gpu,
        'ram': ram,
        'storage': storage,
        'gpu_acceleration': gpu['available'] and gpu['nvenc'],
        'active_task': active_task
    }
