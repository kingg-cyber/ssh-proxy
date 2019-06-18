"""
Used for sshd's AuthorizedKeysCommand to fetch a list of authorized keys.
The script will create a file in ssh's authorized_keys format at `/etc/ssh/authorized_keys_cache` containing
authorized_keys from all ssh target containers / pods (by exec'ing into them and fetching the public keys).
The script will cache the containers / pods it already got the keys from to reduce runtime and will only exec
into those it did not fetch the keys from in a previous run. This behavior can be changed with the first argument arg1.

# Arguments
    arg1 (str):
        If value of arg1 is 'full', then the cache files are not considered

"""


import docker
from kubernetes import client, config, stream
import os, sys
from filelock import FileLock, Timeout
from subprocess import getoutput

SSH_PERMIT_SERVICE_PREFIX = os.getenv("SSH_PERMIT_SERVICE_PREFIX", "")

authorized_keys_cache_file = "/etc/ssh/authorized_keys_cache"
authorized_keys_cache_file_lock = "cache_files.lock"
query_cache_file = "/etc/ssh/query_cache"

container_client = None
CONTAINER_CLIENT_KUBERNETES = "kubernetes"
CONTAINER_CLIENT_DOCKER = "docker"

PRINT_KEY_COMMAND = ["cat", "/root/.ssh/id_ed25519.pub"]

# First try to find Kubernetes client. If Kubernetes client is not there, use the Docker client
try:
    config.load_kube_config()
    kubernetes_client = client.CoreV1Api()
    container_client = CONTAINER_CLIENT_KUBERNETES

    # at this path the namespace the container is in is stored in Kubernetes deployment (see https://stackoverflow.com/questions/31557932/how-to-get-the-namespace-from-inside-a-pod-in-openshift)
    NAMESPACE = getoutput("cat /var/run/secrets/kubernetes.io/serviceaccount/namespace")
except FileNotFoundError:
    try:
        docker_client = docker.from_env()
        docker_client.ping()
        container_client = CONTAINER_CLIENT_DOCKER
    except FileNotFoundError:
        pass

if container_client is None:
    print("Could neither initialize Kubernetes nor Docker client. Stopping execution.")
    exit(1)

def get_authorized_keys_kubernetes(query_cache=[]):
    """
    Execs into all Kubernetes pods where the name starts with `SSH_PERMIT_SERVICE_PREFIX` and returns it's public key.

    # Note: This method can be quite slow. For big setups / clusters, think about rewriting it to fetch public keys from a REST API or so.

    # Arguments
        query_cache (list of str): contains Pod names which are skipped

    # Returns
        authorized_keys (list of str): newly fetched public keys
        new_query_cache (list of str): name of all pods (previously cached ones and newly exec'd ones)

    """
    
    pod_list = kubernetes_client.list_namespaced_pod(NAMESPACE, field_selector="status.phase=Running")
    authorized_keys = []
    new_query_cache = []
    for pod in pod_list.items:
        name = pod.metadata.name

        if name.startswith(SSH_PERMIT_SERVICE_PREFIX) is False:
            continue
        elif name in query_cache:
            new_query_cache.append(name)
            continue
        
        try:
            exec_result = stream.stream(kubernetes_client.connect_get_namespaced_pod_exec, name, NAMESPACE, command=PRINT_KEY_COMMAND, stderr=True, stdin=False, stdout=True, tty=False)
            authorized_keys.append(exec_result)
            new_query_cache.append(name)
        except:
            # This can happen when the pod is in a false state such as Terminating, as status.phase is 'Running' but pod cannot be reached anymore
            print("Could not reach pod {}".format(name))
    
    return authorized_keys, new_query_cache

def get_authorized_keys_docker(query_cache=[]):
    """
    Execs into all Docker containers where the name starts with `SSH_PERMIT_SERVICE_PREFIX` and returns it's public key.

    # Note: This method can be quite slow. For big setups / clusters, think about rewriting it to fetch public keys from a REST API or so.

    # Arguments
        query_cache (list of str): contains container ids which are skipped

    # Returns
        authorized_keys (list of str): newly fetched public keys
        new_query_cache (list of str): ids of all containers (previously cached ones and newly exec'd ones)

    """

    containers = docker_client.containers.list()
    authorized_keys = []
    new_query_cache = []
    for container in containers:
        if container.name.startswith(SSH_PERMIT_SERVICE_PREFIX) is False:
            continue
        elif container.id in query_cache:
            new_query_cache.append(container.id)
            continue
        
        exec_result = container.exec_run(PRINT_KEY_COMMAND)
        authorized_keys.append(exec_result[1].decode("utf-8"))
        new_query_cache.append(container.id)

    return authorized_keys, new_query_cache

def update_cache_file():
    # make sure only a single script execution can update authorized_keys file
    lock = FileLock(authorized_keys_cache_file_lock, timeout=0)
    try:
        with lock:
            write_mode = 'a'
            # Delete query_cache file in case it is a 'full' run
            if len(sys.argv) == 2 and sys.argv[1] == "full":
                os.remove(query_cache_file)
                write_mode = 'w'

            query_cache = []
            if os.path.isfile(query_cache_file):
                with open(query_cache_file, 'r') as cache_file:
                    for line in cache_file.readlines():
                        # the strip will remove the newline character at the end of each line
                        query_cache.append(line.strip())

            if container_client == CONTAINER_CLIENT_DOCKER:
                authorized_keys, new_query_cache = get_authorized_keys_docker(query_cache=query_cache)
            elif container_client == CONTAINER_CLIENT_KUBERNETES:
                authorized_keys, new_query_cache = get_authorized_keys_kubernetes(query_cache=query_cache)
            
            with open(authorized_keys_cache_file, write_mode) as cache_file:
                for authorized_key in authorized_keys:
                    if authorized_key.startswith("ssh") == False:
                        continue
                    
                    cache_file.write("{}\n".format(authorized_key))
            
            with open(query_cache_file, 'w') as cache_file:
                for entry in new_query_cache:
                    cache_file.write("{}\n".format(entry))

    except Timeout:
        # The cache is currently updated by someone else
        print("The cache is currently updated by someone else")
        pass


if __name__ == "__main__":
    update_cache_file()