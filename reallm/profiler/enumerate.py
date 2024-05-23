import time

from reallm.profiler.device_mesh import *
from reallm.profiler.estimate import estimate_rpc_memory, estimate_rpc_time
from reallm.profiler.experiments import *
from reallm.profiler.rpc import *
import reallm.api.core.dfg

GPU_MEM_CAP = 80 * (1024**3)
MEM_INDEX = 1.0


def enumerate_rpc_executions(rpc: ModelRPC, device_mesh: DeviceMesh, num_gen_tokens,
                             n_ppo_minibatches) -> List[RPCExecution]:
    sub_device_meshes = find_sub_device_meshes(device_mesh)
    feasible = []
    # mem_index = 1.2
    for sub_device_mesh in sub_device_meshes:
        ps = find_parallel_strategies(sub_device_mesh)
        for p in ps:
            bs = rpc.min_n_seqs
            seq_len = rpc.max_n_tokens // bs  #FIXME:
            min_bs = 2 * p.num_dp * p.num_pp * n_ppo_minibatches\
                if rpc.interface_type == ModelInterfaceType.TRAIN_STEP else p.num_dp * p.num_pp
            if min_bs > bs:
                # batch size too small
                continue
            if p.num_mp * p.num_dp > 8 and rpc.interface_type == ModelInterfaceType.TRAIN_STEP:
                continue
            if p.num_pp > max(device_mesh.n_nodes, 8):
                continue
            mem_cost, static_mem = estimate_rpc_memory(rpc,
                                                       p,
                                                       bs,
                                                       seq_len,
                                                       n_ppo_minibatches=n_ppo_minibatches,
                                                       gen_len=num_gen_tokens,
                                                       offload=rpc.model_name.role in ["ref", "reward"])
            mem_cost = int(mem_cost * MEM_INDEX)
            static_mem = int(static_mem * MEM_INDEX)
            time_cost = estimate_rpc_time(rpc,
                                          p,
                                          bs=bs,
                                          seq_len=seq_len,
                                          num_gen_tokens=num_gen_tokens,
                                          use_gradient_checkpointing=True,
                                          n_ppo_minibatches=n_ppo_minibatches)
            time_cost = int(time_cost)
            if mem_cost < GPU_MEM_CAP:
                rep_rpc = RPC.from_config(rpc)
                feasible.append(RPCExecution(rep_rpc, sub_device_mesh, p, time_cost, mem_cost, static_mem))
    return feasible


def build_graph(rpcs: List[ModelRPC], num_epoch: int = 5, epoch_dependency_interval: int = 1, if_print=False):
    """ Build model function call graph of multiple training epochs,
    
    args:
        exp: ProfileExperiment, the experiment object
        num_epoch: int, number of training epochs
        epoch_dependency_interval: int, the interval of epoch dependency, 
            e.g. if epoch_dependency_interval = 2, then the graph will have 
            edges between epoch i and epoch i+2, i+4, ...
    """
    # one epoch dependency graph
    rpcs, edges = reallm.api.core.dfg.build_graph(rpcs)
    rpc_names_mapping = {rpc.name: rpc for rpc in rpcs}
    rpc_instances = []

    # multi epoch graph
    for epoch_id in range(num_epoch):
        for rpc in rpcs:
            children = []
            parents = []
            if rpc.is_src and epoch_id >= epoch_dependency_interval:
                for other in rpcs:
                    if other.is_dst:
                        parents.append(
                            RPCInstance(RPC.from_config(other), epoch_id - epoch_dependency_interval, [], []))
            if rpc.is_dst:
                for other in rpcs:
                    if other.is_src and epoch_id + epoch_dependency_interval < num_epoch:
                        children.append(
                            RPCInstance(RPC.from_config(other), epoch_id + epoch_dependency_interval, [], []))
            for parent in rpc.parents:
                p = RPC.from_config(rpc_names_mapping[parent])
                parents.append(RPCInstance(p, epoch_id, [], []))
            for child in rpc.children:
                c = RPC.from_config(rpc_names_mapping[child])
                children.append(RPCInstance(c, epoch_id, [], []))
            rpc_instance = RPCInstance(RPC.from_config(rpc), epoch_id, parents, children)
            rpc_instances.append(rpc_instance)
    if if_print:
        for ri in rpc_instances:
            print(ri)
    return rpc_instances


if __name__ == "__main__":
    exp = ProfileExperiment()
    # enumerate_model_device_mappings(exp)
    rpc_instances = build_graph(exp)
