# accurate_cpu_monitor.py
from locust import HttpUser, task, between, events
import psutil
import matplotlib.pyplot as plt
import time
from threading import Thread
import numpy as np
import os
from datetime import datetime

class AccurateCPUMonitor:
    """
    CPU Monitor that matches top command output
    """
    def __init__(self, process_name="uvicorn", port=8000):
        self.process = None
        self.process_name = process_name
        self.port = port
        self.cpu_percentages = []
        self.timestamps = []
        self.monitoring = True
        self.thread = None
        
        # Store previous CPU times for accurate calculation
        self.prev_cpu_times = None
        self.prev_time = None
        
        # For system CPU tracking
        self.system_cpu_percentages = []
        
        self.find_process()
        
    def find_process(self):
        """Find the FastAPI process"""
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                if 'uvicorn' in cmdline.lower() and f':{self.port}' in cmdline:
                    self.process = proc
                    print(f"Found FastAPI process: PID {proc.pid}")
                    
                    # Get initial CPU times
                    self.prev_cpu_times = self.process.cpu_times()
                    self.prev_time = time.time()
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        print("Warning: FastAPI process not found")
    
    def calculate_cpu_percent(self):
        """
        Calculate CPU percentage the same way top does:
        (CPU time difference / elapsed time) * 100
        """
        if not self.process:
            return 0
        
        try:
            # Get current CPU times
            current_cpu_times = self.process.cpu_times()
            current_time = time.time()
            
            # Calculate differences
            if self.prev_cpu_times and self.prev_time:
                user_diff = current_cpu_times.user - self.prev_cpu_times.user
                system_diff = current_cpu_times.system - self.prev_cpu_times.system
                total_cpu_diff = user_diff + system_diff
                
                time_diff = current_time - self.prev_time
                
                # Calculate percentage (CPU time / elapsed time * 100)
                if time_diff > 0:
                    cpu_percent = (total_cpu_diff / time_diff) * 100
                else:
                    cpu_percent = 0
            else:
                cpu_percent = 0
            
            # Update previous values
            self.prev_cpu_times = current_cpu_times
            self.prev_time = current_time
            
            return cpu_percent
            
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            print(f"Error calculating CPU: {e}")
            return 0
    
    def get_process_info(self):
        """Get detailed process information"""
        if not self.process:
            return None
        
        try:
            info = {
                'pid': self.process.pid,
                'name': self.process.name(),
                'status': self.process.status(),
                'cpu_percent': self.calculate_cpu_percent(),
                'memory_percent': self.process.memory_percent(),
                'memory_rss': self.process.memory_info().rss / 1024 / 1024,  # MB
                'memory_vms': self.process.memory_info().vms / 1024 / 1024,  # MB
                'num_threads': self.process.num_threads(),
                'connections': len(self.process.connections()),
                'create_time': datetime.fromtimestamp(self.process.create_time()).strftime('%H:%M:%S')
            }
            return info
        except Exception as e:
            print(f"Error getting process info: {e}")
            return None
    
    def monitor(self):
        """Monitor CPU usage continuously"""
        if not self.process:
            print("No process to monitor")
            return
        
        start_time = time.time()
        print(f"\nStarting accurate CPU monitoring for PID {self.process.pid}")
        print(f"{'Time':>8} {'CPU%':>8} {'Memory(MB)':>12} {'Threads':>8} {'Status':>10}")
        print("-" * 50)
        
        while self.monitoring:
            try:
                current_time = time.time() - start_time
                
                # Get process info
                info = self.get_process_info()
                
                if info:
                    # Store data
                    self.timestamps.append(current_time)
                    self.cpu_percentages.append(info['cpu_percent'])
                    
                    # Also get system CPU for comparison
                    system_cpu = psutil.cpu_percent(interval=None)  # Instantaneous
                    self.system_cpu_percentages.append(system_cpu)
                    
                    # Print real-time stats every 2 seconds
                    if len(self.timestamps) % 4 == 0:  # Every ~2 seconds
                        print(f"{current_time:8.1f} {info['cpu_percent']:8.1f} "
                              f"{info['memory_rss']:12.1f} {info['num_threads']:8d} "
                              f"{info['status']:10}")
                
                time.sleep(0.5)  # Sample every 0.5 seconds
                
            except Exception as e:
                print(f"Monitoring error: {e}")
                break
    
    def start(self):
        """Start monitoring thread"""
        if self.thread and self.thread.is_alive():
            return
        
        self.thread = Thread(target=self.monitor)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self):
        """Stop monitoring"""
        self.monitoring = False
        if self.thread:
            self.thread.join(timeout=2)
    
    def plot_results(self):
        """Plot CPU usage comparison"""
        if not self.cpu_percentages:
            print("No data collected")
            return
        
        fig, axes = plt.subplots(3, 1, figsize=(14, 10))
        
        # Plot 1: Process CPU vs System CPU
        axes[0].plot(self.timestamps, self.cpu_percentages, 'b-', linewidth=2, 
                    label=f'Process CPU (avg: {np.mean(self.cpu_percentages):.1f}%)')
        axes[0].plot(self.timestamps, self.system_cpu_percentages, 'r-', linewidth=1, alpha=0.7,
                    label=f'System CPU (avg: {np.mean(self.system_cpu_percentages):.1f}%)')
        axes[0].set_xlabel('Time (seconds)')
        axes[0].set_ylabel('CPU Usage (%)')
        axes[0].set_title(f'CPU Usage Comparison - PID {self.process.pid if self.process else "N/A"}')
        axes[0].grid(True, alpha=0.3)
        axes[0].legend()
        axes[0].set_ylim(0, max(100, max(self.cpu_percentages) * 1.1))
        
        # Plot 2: CPU Distribution
        axes[1].hist(self.cpu_percentages, bins=30, edgecolor='black', alpha=0.7, color='blue')
        axes[1].axvline(np.mean(self.cpu_percentages), color='r', linestyle='--', 
                       linewidth=2, label=f'Mean: {np.mean(self.cpu_percentages):.1f}%')
        axes[1].axvline(np.median(self.cpu_percentages), color='g', linestyle='--',
                       linewidth=2, label=f'Median: {np.median(self.cpu_percentages):.1f}%')
        axes[1].set_xlabel('CPU Usage (%)')
        axes[1].set_ylabel('Frequency')
        axes[1].set_title('Process CPU Usage Distribution')
        axes[1].legend()
        axes[1].grid(True, alpha=0.3)
        
        # Plot 3: CPU usage percentiles
        percentiles = [10, 25, 50, 75, 90, 95, 99]
        percentile_values = [np.percentile(self.cpu_percentages, p) for p in percentiles]
        
        axes[2].bar(range(len(percentiles)), percentile_values, tick_label=[f'{p}th' for p in percentiles])
        axes[2].set_xlabel('Percentile')
        axes[2].set_ylabel('CPU Usage (%)')
        axes[2].set_title('CPU Usage Percentiles')
        axes[2].grid(True, alpha=0.3)
        
        # Add value labels on bars
        for i, v in enumerate(percentile_values):
            axes[2].text(i, v + 1, f'{v:.1f}%', ha='center', va='bottom')
        
        plt.tight_layout()
        
        # Save plot
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accurate_cpu_monitor_{timestamp}.png"
        plt.savefig(filename, dpi=300, bbox_inches='tight')
        print(f"\nPlot saved as {filename}")
        
        # Print detailed statistics
        print("\n" + "="*60)
        print("CPU USAGE STATISTICS (Process-Specific)")
        print("="*60)
        print(f"  Average CPU:     {np.mean(self.cpu_percentages):.2f}%")
        print(f"  Median CPU:      {np.median(self.cpu_percentages):.2f}%")
        print(f"  Maximum CPU:     {np.max(self.cpu_percentages):.2f}%")
        print(f"  Minimum CPU:     {np.min(self.cpu_percentages):.2f}%")
        print(f"  Std Deviation:   {np.std(self.cpu_percentages):.2f}%")
        print(f"  95th Percentile: {np.percentile(self.cpu_percentages, 95):.2f}%")
        print(f"  99th Percentile: {np.percentile(self.cpu_percentages, 99):.2f}%")
        print(f"  Samples:         {len(self.cpu_percentages)}")
        
        print("\n" + "="*60)
        print("SYSTEM CPU STATISTICS (For Comparison)")
        print("="*60)
        print(f"  Average System CPU: {np.mean(self.system_cpu_percentages):.2f}%")
        print(f"  Max System CPU:     {np.max(self.system_cpu_percentages):.2f}%")
        
        plt.show()

# Locust integration
monitor = None

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    global monitor
    monitor = AccurateCPUMonitor(process_name="uvicorn", port=8000)
    monitor.start()
    print("Accurate CPU Monitor started")

@events.quitting.add_listener
def on_locust_quitting(environment, **kwargs):
    global monitor
    if monitor:
        monitor.stop()
        monitor.plot_results()
        print("Accurate CPU Monitor stopped")

class RAGUser(HttpUser):
    wait_time = between(0.5, 2)
    
    @task
    def query_rag(self):
        self.client.post(
            "/query",
            json={"query": "What is machine learning?"},
            timeout=10
        )
