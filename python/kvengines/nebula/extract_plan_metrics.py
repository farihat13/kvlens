"""
Extract plan metrics from NebulaGraph plan files.

Usage:
    python -m kvengines.nebula.extract_plan_metrics --logs-dir ../logs --output metrics.csv
    python -m kvengines.nebula.extract_plan_metrics --logs-dir ../logs --output metrics.csv --with-paths
    python -m kvengines.nebula.extract_plan_metrics --list-fields
    python -m kvengines.nebula.extract_plan_metrics --export-details --visualize
"""

import argparse
import os
import re
import json
from pathlib import Path
import pandas as pd

# Import NebulaGraph types needed for eval()
from nebula3.graph import ttypes as GraphTtypes
from nebula3.common import ttypes as CommonTtypes

# Import Graphviz for visualization
try:
    from graphviz import Digraph
    GRAPHVIZ_AVAILABLE = True
except ImportError:
    GRAPHVIZ_AVAILABLE = False
    print("Warning: graphviz not available. Install with: pip install graphviz")


verbose = False


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_all_storage_metrics(profile):
    """Extract all storage-related metrics from profile"""
    metrics = {
        'storage_exec_us': 0.0,
        'storage_total_us': 0.0,
        'kvstore_maxtime_us': 0.0,
        'kvstore_totaltime_us': 0.0,
        'storage_details': []  # For display purposes
    }
    
    if not hasattr(profile, 'other_stats') or not profile.other_stats:
        return metrics
    
    for key, value in profile.other_stats.items():
        key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
        value_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
        
        # Extract storage exec time
        storage_exec_us = 0.0
        if key_str.startswith('step[') or 'storage' in value_str.lower():
            try:
                json_obj = json.loads(value_str)
                
                if 'storage' in json_obj and isinstance(json_obj['storage'], list):
                    for storage_entry in json_obj['storage']:
                        if 'exec' in storage_entry:
                            match = re.search(r'(\d+)\(us\)', storage_entry['exec'])
                            if match:
                                val = float(match.group(1))
                                metrics['storage_exec_us'] += val
                                storage_exec_us = val
                
                if 'exec' in json_obj and isinstance(json_obj['exec'], str):
                    match = re.search(r'(\d+)\(us\)', json_obj['exec'])
                    if match:
                        val = float(match.group(1))
                        metrics['storage_exec_us'] += val
                        storage_exec_us = val
                        
            except json.JSONDecodeError:
                pass
        
        # Extract storage total time
        if '"total"' in value_str:
            matches = re.findall(r'"total":\s*"(\d+)\(us\)"', value_str)
            for match_value in matches:
                val = float(match_value)
                metrics['storage_total_us'] += val
                metrics['storage_details'].append(f"storage_total: {match_value}(us)")
        
        # Extract kvstore max time
        if 'max_kvstore_time_us' in value_str:
            match = re.search(r'"max_kvstore_time_us":\s*"(\d+)\(us\)"', value_str)
            if match:
                val = float(match.group(1))
                metrics['kvstore_maxtime_us'] += val
                metrics['storage_details'].append(f"kvstore_max: {match.group(1)}(us)")
                if verbose and val > 100_000:
                    print(f"\t\tFound kvstore_maxtime_us:   {val:10.1f}(us)")

        # Extract kvstore total time
        if 'total_kvstore_time_us' in value_str:
            match = re.search(r'"total_kvstore_time_us":\s*"(\d+)\(us\)"', value_str)
            if match:
                val = float(match.group(1))
                metrics['kvstore_totaltime_us'] += val
                metrics['storage_details'].append(f"kvstore_total: {match.group(1)}(us)")
    
    return metrics


def build_plan_graph(plan_desc):
    """Build graph structure from plan description"""
    graph = {}
    reverse_graph = {}
    all_nodes = {}
    
    # Initialize
    for node in plan_desc.plan_node_descs:
        node_id = node.id
        all_nodes[node_id] = node
        reverse_graph[node_id] = []
        graph[node_id] = []
    
    # Build edges
    for node in plan_desc.plan_node_descs:
        node_id = node.id
        if hasattr(node, 'dependencies') and node.dependencies:
            graph[node_id] = list(node.dependencies)
            for dep_id in node.dependencies:
                reverse_graph[dep_id].append(node_id)
    
    # Find start and end nodes
    start_nodes = [nid for nid in all_nodes.keys() if len(reverse_graph[nid]) == 0]
    end_nodes = [nid for nid in all_nodes.keys() if len(graph[nid]) == 0]
    
    return graph, reverse_graph, all_nodes, start_nodes, end_nodes


def get_node_name(node):
    """Get decoded node name"""
    return node.name.decode() if isinstance(node.name, bytes) else node.name


def export_profiling_stats_to_json(filepath, output_dir='./logs/plan_details'):
    """
    Export raw ProfilingStats from plan file to JSON format.
    Faithfully preserves all information from ProfilingStats without interpretation.
    
    Args:
        filepath: Path to the plan file
        output_dir: Directory to save the JSON
        
    Returns:
        Path to the created JSON file or None on error
    """
    filename = os.path.basename(filepath)
    match = re.match(r'(.+)_plan\.txt', filename)
    
    if not match:
        if verbose:
            print(f"Skipping {filename}: doesn't match expected pattern")
        return None
    
    plan_desc, latency = parse_plan_file(filepath)
    if plan_desc is None:
        return None
    
    # Create output directory if it doesn't exist
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    query_name = match.group(1)
    
    # Build JSON structure - focus on ProfilingStats
    plan_json = {
        'query': query_name,
        'latency_us': latency,
        'optimize_time_us': plan_desc.optimize_time_in_us,
        'total_nodes': len(plan_desc.plan_node_descs),
        'nodes': []
    }
    
    # Extract each node's information
    for node in plan_desc.plan_node_descs:
        node_data = {
            'id': node.id,
            'name': get_node_name(node),
        }
        
        # Add dependencies
        if hasattr(node, 'dependencies') and node.dependencies:
            node_data['dependencies'] = list(node.dependencies)
        else:
            node_data['dependencies'] = []
        
        # Add ProfilingStats if present
        if node.profiles and len(node.profiles) > 0:
            profile = node.profiles[0]
            
            profiling_stats = {
                'rows': profile.rows,
                'exec_duration_in_us': profile.exec_duration_in_us,
                'total_duration_in_us': profile.total_duration_in_us,
            }
            
            # Add other_stats if present - preserve exactly as is
            if hasattr(profile, 'other_stats') and profile.other_stats:
                profiling_stats['other_stats'] = {}
                
                for key, value in profile.other_stats.items():
                    # Decode bytes to strings
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                    value_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                    
                    # Try to parse as JSON for structured data, otherwise keep as string
                    try:
                        profiling_stats['other_stats'][key_str] = json.loads(value_str)
                    except json.JSONDecodeError:
                        profiling_stats['other_stats'][key_str] = value_str
            
            node_data['profiling_stats'] = profiling_stats
        else:
            node_data['profiling_stats'] = None
        
        plan_json['nodes'].append(node_data)
    
    # Save JSON with pretty printing
    output_file = output_path / f"{query_name}_profiling.json"
    with open(output_file, 'w') as f:
        json.dump(plan_json, f, indent=4)
    
    if verbose:
        print(f"Exported ProfilingStats JSON: {output_file}")
    
    return output_file


# ============================================================================
# VISUALIZATION
# ============================================================================

def visualize_plan_graphviz(plan_desc, output_file='plan'):
    """Create a visual graph of the execution plan"""
    if not GRAPHVIZ_AVAILABLE:
        print("Warning: Graphviz not available, skipping visualization")
        return None
    
    dot = Digraph(comment='Execution Plan')
    dot.attr(rankdir='TB')
    dot.attr('node', fontsize='10')
    
    # Summary metrics
    summary = {
        'total_time_us': 0.0,
        'total_exec_us': 0.0,
        'total_storage_total_us': 0.0,
        'total_storage_exec_us': 0.0,
        'total_kvstore_max_us': 0.0,
        'total_kvstore_total_us': 0.0,
    }
    
    # Add nodes
    for node in plan_desc.plan_node_descs:
        name = get_node_name(node)
        
        if node.profiles:
            profile = node.profiles[0]
            storage_metrics = extract_all_storage_metrics(profile)
            
            # Update summary
            summary['total_time_us'] += profile.total_duration_in_us
            summary['total_exec_us'] += profile.exec_duration_in_us
            summary['total_storage_total_us'] += storage_metrics['storage_total_us']
            summary['total_storage_exec_us'] += storage_metrics['storage_exec_us']
            summary['total_kvstore_max_us'] += storage_metrics['kvstore_maxtime_us']
            summary['total_kvstore_total_us'] += storage_metrics['kvstore_totaltime_us']
            
            # Build label
            time_ms = profile.exec_duration_in_us / 1000
            total_time_ms = profile.total_duration_in_us / 1000
            
            label = f"{name} ({node.id})\nRows: {profile.rows:,}\n"
            label += f"Total: {total_time_ms:.1f}ms\n"
            label += f"Exec: {time_ms:.1f}ms"
            
            # Add storage metrics if significant
            if storage_metrics['storage_exec_us'] > 1000:  # > 1ms
                label += f"\nStorage Exec: {storage_metrics['storage_exec_us']/1000:.1f}ms"
            
            if storage_metrics['kvstore_maxtime_us'] > 1000:  # > 1ms
                label += f"\nKVStore Max: {storage_metrics['kvstore_maxtime_us']/1000:.1f}ms"
            
            # Color by execution time
            if total_time_ms > 500:
                color = 'red'
            elif total_time_ms > 100:
                color = 'orange'
            elif total_time_ms > 10:
                color = 'yellow'
            else:
                color = 'lightblue'
            
            dot.node(str(node.id), label, style='filled', fillcolor=color)
        else:
            dot.node(str(node.id), f"{name}\nID: {node.id}")
    
    # Add edges
    for node in plan_desc.plan_node_descs:
        if hasattr(node, 'dependencies') and node.dependencies:
            for dep_id in node.dependencies:
                dot.edge(str(node.id), str(dep_id))
    
    # Render
    try:
        dot.render(output_file, format='png', cleanup=True)
        if verbose:
            print(f"  Plan visualization saved to {output_file}.png")
        
        # Print summary
        if verbose:
            print("\n  === Plan Summary ===")
            print(f"  Total Time:         {summary['total_time_us']/1000:7.2f} ms")
            print(f"  Execution Time:     {summary['total_exec_us']/1000:7.2f} ms")
            print(f"  Storage Total:      {summary['total_storage_total_us']/1000:7.2f} ms")
            print(f"  Storage Exec:       {summary['total_storage_exec_us']/1000:7.2f} ms")
            print(f"  KVStore Max:        {summary['total_kvstore_max_us']/1000:7.2f} ms")
            print(f"  KVStore Total:      {summary['total_kvstore_total_us']/1000:7.2f} ms")
        
        return f"{output_file}.png"
    except Exception as e:
        print(f"Warning: Could not create visualization: {e}")
        return None


def visualize_plan_barchart(plan_desc, output_file='plan_breakdown', latency_us=None):
    """Create a horizontal stacked bar chart of time per plan node.

    Segments: KVStore (green), Storage non-KV (orange), Graph exec (blue).
    Sorted by total time descending.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("Warning: matplotlib not available, skipping bar chart")
        return None

    nodes = []
    for node in plan_desc.plan_node_descs:
        if not node.profiles:
            continue
        profile = node.profiles[0]
        sm = extract_all_storage_metrics(profile)
        name = get_node_name(node)
        kv_us = sm['kvstore_totaltime_us']
        storage_non_kv_us = max(0, sm['storage_exec_us'] - kv_us)
        graph_exec_us = max(0, profile.exec_duration_in_us - sm['storage_exec_us'])
        total_us = kv_us + storage_non_kv_us + graph_exec_us
        if total_us < 1:
            continue
        nodes.append({
            'label': f"{name} ({node.id})",
            'kv_ms': kv_us / 1000,
            'storage_ms': storage_non_kv_us / 1000,
            'graph_ms': graph_exec_us / 1000,
            'total_ms': total_us / 1000,
        })

    if not nodes:
        print("Warning: No nodes with timing data for bar chart")
        return None

    nodes.sort(key=lambda n: n['total_ms'])
    labels = [n['label'] for n in nodes]
    kv = [n['kv_ms'] for n in nodes]
    storage = [n['storage_ms'] for n in nodes]
    graph = [n['graph_ms'] for n in nodes]

    fig, ax = plt.subplots(figsize=(10, max(3, len(nodes) * 0.4)))
    y = range(len(labels))
    ax.barh(y, kv, color='#2ca02c', label='KVStore')
    ax.barh(y, storage, left=kv, color='#ff7f0e', label='Storage (non-KV)')
    ax.barh(y, graph, left=[k + s for k, s in zip(kv, storage)],
            color='#1f77b4', label='Graph exec')
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel('Time (ms)')
    title = 'Plan Node Time Breakdown'
    if latency_us:
        title += f' (server latency: {latency_us/1000:.1f}ms)'
    ax.set_title(title)
    ax.legend(loc='lower right', fontsize=8)
    plt.tight_layout()
    out_path = f"{output_file}.png"
    plt.savefig(out_path, dpi=150)
    plt.close()
    return out_path


# ============================================================================
# CSV EXPORT
# ============================================================================

def plan_to_dataframe(plan_desc):
    """Convert execution plan to pandas DataFrame with all important metrics"""
    data = []
    
    for node in plan_desc.plan_node_descs:
        name = get_node_name(node)
        
        if node.profiles:
            profile = node.profiles[0]
            storage_metrics = extract_all_storage_metrics(profile)
            
            row = {
                'ID': node.id,
                'Operator': name,
                'Rows': profile.rows,
                'Exec (ms)': profile.exec_duration_in_us / 1000,
                'Total (ms)': profile.total_duration_in_us / 1000,
                'KVStore Max (ms)': storage_metrics['kvstore_maxtime_us']/1000,
                'KVStore Total (ms)': storage_metrics['kvstore_totaltime_us']/1000,
                'Storage Exec (ms)': storage_metrics['storage_exec_us'] / 1000,
                'Storage Total (ms)': storage_metrics['storage_total_us'] / 1000,
            }
            
            # Add dependencies info
            if hasattr(node, 'dependencies') and node.dependencies:
                row['Dependencies'] = ','.join(map(str, node.dependencies))
            else:
                row['Dependencies'] = ''
            
            data.append(row)
        else:
            # Node without profile
            data.append({
                'ID': node.id,
                'Operator': name,
                'Rows': 0,
                'Exec (ms)': 0,
                'Total (ms)': 0,
                'KVStore Max (ms)': 0,
                'KVStore Total (ms)': 0,
                'Storage Exec (ms)': 0,
                'Storage Total (ms)': 0,
                'Dependencies': ','.join(map(str, node.dependencies)) if hasattr(node, 'dependencies') and node.dependencies else ''
            })
    
    df = pd.DataFrame(data)
    # Sort by Total time descending
    df = df.sort_values('Total (ms)', ascending=False)
    return df


def export_plan_to_csv(filepath, output_dir='./logs/plan_details', visualize=False):
    """
    Export a single plan file to a detailed CSV and optionally create visualization.
    
    Args:
        filepath: Path to the plan file
        output_dir: Directory to save the CSV
        visualize: If True, create PNG visualization
        
    Returns:
        Path to the created CSV file or None on error
    """
    filename = os.path.basename(filepath)
    match = re.match(r'(.+)_plan\.txt', filename)
    
    if not match:
        print(f"Skipping {filename}: doesn't match expected pattern")
        return None
    
    plan_desc, latency = parse_plan_file(filepath)
    if plan_desc is None:
        return None
    
    # Create output directory if it doesn't exist
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    query_name = match.group(1)
    
    # Create DataFrame from plan
    df = plan_to_dataframe(plan_desc)
    
    # Add metadata columns at the beginning
    df.insert(0, 'Latency (us)', latency)
    df.insert(1, 'Optimize Time (us)', plan_desc.optimize_time_in_us)
    
    # Save CSV
    output_file = output_path / f"{query_name}_details.csv"
    df.to_csv(output_file, index=False)
    
    if verbose:
        print(f"Exported CSV: {output_file}")
    
    # Create visualization if requested
    if visualize:
        viz_output = output_path / f"{query_name}_plan"
        visualize_plan_graphviz(plan_desc, output_file=str(viz_output))
    
    return output_file



def export_all_plans_to_csv(logs_dir, output_dir='./logs/plan_details', 
                            pattern='plan_interactive-complex-*.gql_param*.txt',
                            visualize=False, export_profiling_json=False):
    """
    Export all plan files in a directory to individual CSV files and optionally visualizations.
    
    Args:
        logs_dir: Directory containing plan files
        output_dir: Directory to save CSV files and visualizations
        pattern: File pattern to match
        visualize: If True, create PNG visualizations for each plan
        export_profiling_json: If True, export raw ProfilingStats to JSON format
        
    Returns:
        Tuple of (csv_files, profiling_json_files)
    """
    logs_path = Path(logs_dir)
    plan_files = sorted(logs_path.glob(pattern))
    
    print(f"Found {len(plan_files)} plan files in {logs_dir}")
    print(f"Exporting to {output_dir}")
    if visualize:
        if GRAPHVIZ_AVAILABLE:
            print("Visualization: ENABLED")
        else:
            print("Visualization: DISABLED (graphviz not available)")
            visualize = False
            exit(1)
    if export_profiling_json:
        print("ProfilingStats JSON Export: ENABLED")
    
    csv_files = []
    profiling_json_files = []
    
    for i, filepath in enumerate(plan_files, 1):
        if i % 10 == 0:
            print(f"Processing file {i}/{len(plan_files)}...")
        
        # Export CSV
        csv_file = export_plan_to_csv(filepath, output_dir, visualize)
        if csv_file:
            csv_files.append(csv_file)
        
        # Export ProfilingStats JSON if requested
        if export_profiling_json:
            profiling_json_dir = Path(output_dir).parent / 'plan_details'
            profiling_json_file = export_profiling_stats_to_json(filepath, str(profiling_json_dir))
            if profiling_json_file:
                profiling_json_files.append(profiling_json_file)
    
    print(f"\n✓ Exported {len(csv_files)} plan files to CSV")
    if visualize and GRAPHVIZ_AVAILABLE:
        print(f"✓ Created {len(csv_files)} visualizations")
    if export_profiling_json:
        print(f"✓ Exported {len(profiling_json_files)} ProfilingStats JSON files")
    
    return csv_files, profiling_json_files


# ============================================================================
# FIELD DEFINITIONS - MODIFY THESE TO ADD/REMOVE FIELDS
# ============================================================================

EXTRACTABLE_FIELDS = {
    # Basic info
    'query': {
        'description': 'Query number',
        'category': 'basic',
        'extractor': lambda plan_desc, query_num, param_num: query_num
    },
    'param': {
        'description': 'Parameter set number',
        'category': 'basic',
        'extractor': lambda plan_desc, query_num, param_num: param_num
    },
    
    # Plan-level metrics
    'total_nodes': {
        'description': 'Total number of nodes in plan',
        'category': 'basic',
        'extractor': lambda plan_desc, query_num, param_num: len(plan_desc.plan_node_descs)
    },
    'optimize_time_us': {
        'description': 'Optimization time in microseconds',
        'category': 'basic',
        'extractor': lambda plan_desc, query_num, param_num: plan_desc.optimize_time_in_us
    },
    
    # Time metrics (microseconds)
    'total_time_us': {
        'description': 'Sum of total_duration_in_us across all nodes',
        'category': 'time',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            node.profiles[0].total_duration_in_us if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
    'total_exec_us': {
        'description': 'Sum of exec_duration_in_us across all nodes',
        'category': 'time',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            node.profiles[0].exec_duration_in_us if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
    
    # Storage metrics (microseconds)
    'total_storage_exec_us': {
        'description': 'Sum of storage_exec_us across all nodes',
        'category': 'storage',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            extract_all_storage_metrics(node.profiles[0])['storage_exec_us'] if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
    'total_storage_total_us': {
        'description': 'Sum of storage_total_us across all nodes',
        'category': 'storage',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            extract_all_storage_metrics(node.profiles[0])['storage_total_us'] if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
    'total_kvstore_maxtime_us': {
        'description': 'Sum of max_kvstore_time_us across all nodes',
        'category': 'storage',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            extract_all_storage_metrics(node.profiles[0])['kvstore_maxtime_us'] if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
    'total_kvstore_totaltime_us': {
        'description': 'Sum of total_kvstore_time_us across all nodes',
        'category': 'storage',
        'extractor': lambda plan_desc, query_num, param_num: sum(
            extract_all_storage_metrics(node.profiles[0])['kvstore_totaltime_us'] if node.profiles else 0
            for node in plan_desc.plan_node_descs
        )
    },
}


PATH_FIELDS = {
    'total_paths': {
        'description': 'Number of paths analyzed',
        'category': 'path',
        'extractor': lambda path_analysis: len(path_analysis['all_paths'])
    },
    
    # Path metrics (microseconds)
    'maxpath_total_time_us': {
        'description': 'Maximum total time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['total_time']['value']
    },
    'maxpath_exec_time_us': {
        'description': 'Maximum exec time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['exec_time']['value']
    },
    'maxpath_storage_total_us': {
        'description': 'Maximum storage total time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['storage_total']['value']
    },
    'maxpath_storage_exec_us': {
        'description': 'Maximum storage exec time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['storage_exec']['value']
    },
    'maxpath_kvstore_maxtime_us': {
        'description': 'Maximum kvstore max time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['kvstore_maxtime']['value']
    },
    'maxpath_kvstore_totaltime_us': {
        'description': 'Maximum kvstore total time across all paths (us)',
        'category': 'path',
        'extractor': lambda path_analysis: path_analysis['max_metrics']['kvstore_totaltime']['value']
    },
}


# ============================================================================
# PATH ANALYSIS
# ============================================================================

def analyze_paths_for_plan(plan_desc):
    """Run path analysis on a plan"""
    graph, reverse_graph, all_nodes, start_nodes, end_nodes = build_plan_graph(plan_desc)
    
    def dfs(node_id, path, visited):
        path.append(node_id)
        visited.add(node_id)
        
        if not graph[node_id]:
            yield list(path)
        else:
            for child_id in graph[node_id]:
                if child_id not in visited:
                    yield from dfs(child_id, path, visited)
        
        path.pop()
        visited.remove(node_id)
    
    def calculate_path_metrics(path):
        metrics = {
            'total_time_us': 0.0,
            'exec_time_us': 0.0,
            'storage_total_us': 0.0,
            'storage_exec_us': 0.0,
            'kvstore_maxtime_us': 0.0,
            'kvstore_totaltime_us': 0.0,
        }
        
        for node_id in path:
            node = all_nodes[node_id]
            
            if node.profiles:
                profile = node.profiles[0]
                metrics['total_time_us'] += profile.total_duration_in_us
                metrics['exec_time_us'] += profile.exec_duration_in_us
                
                storage_metrics = extract_all_storage_metrics(profile)
                metrics['storage_total_us'] += storage_metrics['storage_total_us']
                metrics['storage_exec_us'] += storage_metrics['storage_exec_us']
                metrics['kvstore_maxtime_us'] += storage_metrics['kvstore_maxtime_us']
                metrics['kvstore_totaltime_us'] += storage_metrics['kvstore_totaltime_us']
        
        return metrics
    
    # Find all paths and track maximums
    all_paths = []
    max_metrics = {
        'total_time': {'value': 0.0, 'path': None},
        'exec_time': {'value': 0.0, 'path': None},
        'storage_total': {'value': 0.0, 'path': None},
        'storage_exec': {'value': 0.0, 'path': None},
        'kvstore_maxtime': {'value': 0.0, 'path': None},
        'kvstore_totaltime': {'value': 0.0, 'path': None},
    }
    
    for start_id in start_nodes:
        for path in dfs(start_id, [], set()):
            metrics = calculate_path_metrics(path)
            
            path_info = {'path': path, **metrics}
            all_paths.append(path_info)
            
            # Track maximums
            for metric_key, metric_name in [
                ('total_time_us', 'total_time'),
                ('exec_time_us', 'exec_time'),
                ('storage_total_us', 'storage_total'),
                ('storage_exec_us', 'storage_exec'),
                ('kvstore_maxtime_us', 'kvstore_maxtime'),
                ('kvstore_totaltime_us', 'kvstore_totaltime'),
            ]:
                if metrics[metric_key] > max_metrics[metric_name]['value']:
                    max_metrics[metric_name] = {'value': metrics[metric_key], 'path': path}
    
    return {
        'all_paths': all_paths,
        'max_metrics': max_metrics
    }


# ============================================================================
# FILE PARSING AND EXTRACTION
# ============================================================================

def parse_plan_file(filepath):
    """Parse a plan file and return the PlanDescription object"""
    with open(filepath, 'r') as f:
        content = f.read()

    # First line contains latency info
    # Latency (us): 229644
    latency = content.splitlines()[0].split(':')[1].strip()
    content = '\n'.join(content.splitlines()[2:])
    
    try:
        # Create namespace with required types for eval()
        namespace = {
            'PlanDescription': GraphTtypes.PlanDescription,
            'PlanNodeDescription': GraphTtypes.PlanNodeDescription,
            'PlanNodeBranchInfo': GraphTtypes.PlanNodeBranchInfo,
            'ProfilingStats': GraphTtypes.ProfilingStats,
            'Pair': GraphTtypes.Pair,
        }
        
        plan_desc = eval(content, namespace)
        return plan_desc, int(latency)
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
        return None, None


def extract_metrics_from_file(filepath, field_names, with_paths=False):
    """Extract metrics from a single plan file"""
    filename = os.path.basename(filepath)
    match = re.match(r'interactive-complex-(\d+)\.gql_param(\d+)_plan\.txt', filename)
    
    if not match:
        print(f"Skipping {filename}: doesn't match expected pattern")
        return None
    
    if verbose:
        print(f"Processing {filename}...")
    
    query_num = int(match.group(1))
    param_num = int(match.group(2))
    
    plan_desc, latency = parse_plan_file(filepath)
    if plan_desc is None:
        return None
    
    metrics = {}
    metrics['latency_us'] = latency
    
    # Extract basic fields
    for field_name in field_names:
        if field_name in EXTRACTABLE_FIELDS:
            try:
                metrics[field_name] = EXTRACTABLE_FIELDS[field_name]['extractor'](
                    plan_desc, query_num, param_num
                )
                if verbose:
                    print(f"\t{field_name}: {metrics[field_name]}")
            except Exception as e:
                print(f"Error extracting {field_name} from {filename}: {e}")
                metrics[field_name] = None
    
    # Extract path analysis fields if requested
    if with_paths:
        try:
            path_analysis = analyze_paths_for_plan(plan_desc)
            
            for field_name in field_names:
                if field_name in PATH_FIELDS:
                    try:
                        metrics[field_name] = PATH_FIELDS[field_name]['extractor'](path_analysis)
                    except Exception as e:
                        print(f"Error extracting path field {field_name} from {filename}: {e}")
                        metrics[field_name] = None

            if verbose:
                for field_name in field_names:
                    if field_name in PATH_FIELDS:
                        print(f"\t{field_name}: {metrics[field_name]}")
        except Exception as e:
            print(f"Error running path analysis for {filename}: {e}")
            for field_name in field_names:
                if field_name in PATH_FIELDS:
                    metrics[field_name] = None
    
    return metrics


def extract_all_metrics(logs_dir, field_names, with_paths=False, pattern='plan_interactive-complex-*.gql_param*.txt'):
    """Extract metrics from all plan files in directory"""
    logs_path = Path(logs_dir)
    plan_files = sorted(logs_path.glob(pattern))
    
    print(f"Found {len(plan_files)} plan files in {logs_dir}")
    
    all_metrics = []
    for i, filepath in enumerate(plan_files, 1):
        if i % 10 == 0:
            print(f"Processing file {i}/{len(plan_files)}...")
        
        metrics = extract_metrics_from_file(filepath, field_names, with_paths)
        if metrics:
            all_metrics.append(metrics)
    
    return all_metrics


# ============================================================================
# MAIN
# ============================================================================


# Update main() to add the flag

def main():
    parser = argparse.ArgumentParser(
        description='Extract metrics from NebulaGraph plan files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available fields:
""" + '\n'.join([f"  {name:30s} - {info['description']}" for name, info in EXTRACTABLE_FIELDS.items()]) +
        '\n\nPath analysis fields (require --with-paths):\n' +
        '\n'.join([f"  {name:30s} - {info['description']}" for name, info in PATH_FIELDS.items()])
    )
    
    parser.add_argument('--logs-dir', help='Directory containing plan files', default='./logs/plan')
    parser.add_argument('--output', help='Output CSV file', default='./logs/nebula_plan_metrics.csv')
    parser.add_argument('--pattern', default='interactive-complex-*.gql_param*_plan.txt',
                        help='File pattern to match')
    parser.add_argument('--fields', nargs='+', 
                        help='Specific fields to extract (default: all)')
    parser.add_argument('--with-paths', action='store_true', default=True,
                        help='Include path analysis metrics')
    parser.add_argument('--list-fields', action='store_true',
                        help='List all available fields and exit')
    parser.add_argument('--export-details', action='store_true', default=True,
                        help='Export each plan to individual detailed CSV files')
    parser.add_argument('--details-dir', default='./logs/plan_details',
                        help='Directory for detailed CSV exports')
    parser.add_argument('--visualize', action='store_true', default=True,
                        help='Create PNG visualizations for each plan (requires graphviz)')
    parser.add_argument('--export-profiling-json', action='store_true', default=True,
                        help='Export raw ProfilingStats to JSON format')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable verbose output')
    
    args = parser.parse_args()

    global verbose
    verbose = args.verbose
    
    # List fields if requested
    if args.list_fields:
        print("\n=== Available Fields ===")
        for name, info in EXTRACTABLE_FIELDS.items():
            print(f"  {name:30s} - {info['description']}")
        
        print("\n=== Path Analysis Fields (require --with-paths) ===")
        for name, info in PATH_FIELDS.items():
            print(f"  {name:30s} - {info['description']}")
        
        return
    
    # Export detailed CSVs if requested
    if args.export_details:
        print("\n=== Exporting Detailed Plan Data ===")
        csv_files, profiling_json_files = export_all_plans_to_csv(
            args.logs_dir, 
            args.details_dir, 
            args.pattern,
            visualize=args.visualize,
            export_profiling_json=args.export_profiling_json
        )
        print(f"✓ Exported {len(csv_files)} detailed CSV files to {args.details_dir}")
        if args.export_profiling_json:
            profiling_json_dir = Path(args.details_dir).parent / 'plan_details'
            print(f"✓ Exported {len(profiling_json_files)} ProfilingStats JSON files to {profiling_json_dir}")
        
        # If only exporting details, return here
        if not args.output:
            return
        print()  # Add blank line before continuing to metrics extraction

    # Validate required arguments for metrics extraction
    if not args.logs_dir or not args.output:
        parser.error("--logs-dir and --output are required (unless using --list-fields or only --export-details)")
    
    # Determine which fields to extract
    if args.fields:
        field_names = args.fields
        all_fields = set(EXTRACTABLE_FIELDS.keys()) | (set(PATH_FIELDS.keys()) if args.with_paths else set())
        invalid_fields = set(field_names) - all_fields
        if invalid_fields:
            print(f"Error: Invalid fields: {', '.join(invalid_fields)}")
            print("Use --list-fields to see available fields")
            return
    else:
        field_names = list(EXTRACTABLE_FIELDS.keys())
        if args.with_paths:
            field_names.extend(PATH_FIELDS.keys())
    
    print(f"\nExtracting fields: {', '.join(field_names)}")
    print(f"From directory: {args.logs_dir}")
    print(f"Pattern: {args.pattern}")
    print(f"With path analysis: {args.with_paths}\n")
    
    # Extract metrics
    all_metrics = extract_all_metrics(args.logs_dir, field_names, args.with_paths, args.pattern)
    
    if not all_metrics:
        print("No metrics extracted!")
        return
    
    # Create DataFrame
    df = pd.DataFrame(all_metrics)
    
    # Sort by query and param
    if 'query' in df.columns and 'param' in df.columns:
        df = df.sort_values(['query', 'param'])
    
    # Save to CSV
    df.to_csv(args.output, index=False)
    
    print(f"\n✓ Extracted {len(all_metrics)} records")
    print(f"✓ Saved to {args.output}")


if __name__ == '__main__':
    main()