# locustfile.py
from locust import HttpUser, task, between
import matplotlib.pyplot as plt
import psutil
import time
from threading import Thread
import numpy as np

class RAGUser(HttpUser):
    wait_time = between(0.5, 2)
    
    def on_start(self):
        """Initialize monitoring when test starts"""
        self.monitor = CPUPerformanceMonitor()
        self.monitor.start()
    
    def on_stop(self):
        """Stop monitoring when test ends"""
        self.monitor.stop()
        self.monitor.plot_results()
    
    @task
    def query_rag(self):
        """Send query to RAG pipeline"""
        self.client.post(
            "/query",
            json={"query": "What is machine learning?"}
        )

class CPUPerformanceMonitor:
    def __init__(self):
        self.cpu_percentages = []
        self.timestamps = []
        self.monitoring = True
        
    def monitor_cpu(self):
        """Monitor CPU in background"""
        process = psutil.Process()
        start_time = time.time()
        
        while self.monitoring:
            cpu_percent = process.cpu_percent(interval=0.5)
            self.cpu_percentages.append(cpu_percent)
            self.timestamps.append(time.time() - start_time)
            time.sleep(0.5)
    
    def start(self):
        """Start monitoring thread"""
        self.thread = Thread(target=self.monitor_cpu)
        self.thread.start()
    
    def stop(self):
        """Stop monitoring"""
        self.monitoring = False
        self.thread.join()
    
    def plot_results(self):
        """Plot CPU usage"""
        plt.figure(figsize=(10, 6))
        plt.plot(self.timestamps, self.cpu_percentages, 'b-', linewidth=2)
        plt.xlabel('Time (seconds)')
        plt.ylabel('CPU Usage (%)')
        plt.title('RAG Pipeline CPU Usage During Load Test')
        plt.grid(True, alpha=0.3)
        
        # Add statistics
        avg_cpu = np.mean(self.cpu_percentages)
        plt.axhline(y=avg_cpu, color='r', linestyle='--', 
                   label=f'Average CPU: {avg_cpu:.1f}%')
        plt.legend()
        
        plt.tight_layout()
        plt.savefig('locust_cpu_usage.png', dpi=300)
        print("CPU usage plot saved as locust_cpu_usage.png")
