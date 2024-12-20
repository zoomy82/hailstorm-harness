import networkx as nx
from hailstorm_harness.framework import HailstormFramework

# Create a graph
graph = nx.Graph()
graph.add_edge(1, 2, weight=1)
graph.add_edge(2, 3, weight=2)
graph.add_edge(3, 4, weight=1)
graph.add_edge(4, 1, weight=3)


# Initialize the framework with a configuration file
config_file = "config.json"
framework = HailstormFramework(graph, config_file)


# Add enhanced agents
framework.add_enhanced_agent(agent_id="Agent1", start_node=1, goal_node=4)
framework.add_enhanced_agent(agent_id="Agent2", start_node=3, goal_node=2)


# Run the simulation
print("Starting simulation...")
framework.run_simulation()
print("Simulation complete!")