"""
    Tests the sFlow feature in SONiC.

    Parameters:
        --enable_sflow_feature: Enable sFlow feature on DUT. Default is disabled
"""

import pytest
import logging
import time
import json
import re

from tests.common.fixtures.ptfhost_utils import copy_ptftests_directory     # noqa F401
from tests.common.fixtures.ptfhost_utils import copy_arp_responder_py       # noqa F401
from tests.ptf_runner import ptf_runner
from tests.common import reboot
from tests.common import config_reload
from tests.common.utilities import wait_until

pytestmark = [
    pytest.mark.topology('t0', 'm0', 'mx')
]

logger = logging.getLogger(__name__)


@pytest.fixture(scope='module', autouse=True)
def setup(duthosts, rand_one_dut_hostname, ptfhost, tbinfo, config_sflow_feature):
    duthost = duthosts[rand_one_dut_hostname]
    global var
    var = {}

    if not check_egress_sflow_capability(duthost):
        pytest.skip("Egress sflow feature is not supported on this platorm")

    feature_status, _ = duthost.get_feature_status()
    if 'sflow' not in feature_status or feature_status['sflow'] == 'disabled':
        pytest.skip("sflow feature is not eanbled")
        
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    var['router_mac'] = duthost.facts['router_mac']
    vlan_dict = mg_facts['minigraph_vlans']
    var['test_ports'] = []
    var['ptf_test_indices'] = []
    var['sflow_ports'] = {}
    var['ingress_sflow_ports'] = {}
    var['egress_sflow_ports'] = {}
    var['ipv4_peer_addrs'] = []
    var['ptfhost'] = ptfhost

    #getting ipv4 peer ip-addresses, which will be used as dst-ip for traffic
    for data in mg_facts['minigraph_bgp']:
        var['ipv4_peer_addrs'].append(data['addr'])
    var['ipv4_peer_addrs'] = var['ipv4_peer_addrs'][0:4]

    for i in range(0, 3, 1):
        var['test_ports'].append(vlan_dict['Vlan1000']['members'][i])
        var['ptf_test_indices'].append(
            mg_facts['minigraph_ptf_indices'][vlan_dict['Vlan1000']['members'][i]])
    collector_ips = ['20.1.1.2', '30.1.1.2']
    var['dut_intf_ips'] = ['20.1.1.1', '30.1.1.1']
    var['mgmt_ip'] = mg_facts['minigraph_mgmt_interface']['addr']
    var['lo_ip'] = mg_facts['minigraph_lo_interfaces'][0]['addr']
    config_dut_ports(duthost, var['test_ports'][0:2], vlan=1000)

    for port_channel, interfaces in list(mg_facts['minigraph_portchannels'].items()):
        port = interfaces['members'][0]
        var['sflow_ports'][port] = {}
        var['sflow_ports'][port]['ifindex'] = get_ifindex(duthost, port)
        var['sflow_ports'][port]['port_index'] = get_port_index(duthost, port)
        var['sflow_ports'][port]['ptf_indices'] = mg_facts['minigraph_ptf_indices'][interfaces['members'][0]]
        var['sflow_ports'][port]['sample_rate'] = 512

    #separating ingress and egress ports
    for port in list(var['sflow_ports'])[0:3]:
        var['ingress_sflow_ports'][port] = {}
        var['ingress_sflow_ports'][port].update(var['sflow_ports'][port])

    for port in list(var['sflow_ports'])[-1:]:
        var['egress_sflow_ports'][port] = {}
        var['egress_sflow_ports'][port].update(var['sflow_ports'][port])

    var['portmap'] = json.dumps(var['sflow_ports'])
    #var['portmap'] = json.dumps(var['ingress_sflow_ports'])

    udp_port = 6343
    for i in range(0, 2, 1):
        var['collector%s' % i] = {}
        var['collector%s' % i]['name'] = 'collector%s' % i
        var['collector%s' % i]['ip_addr'] = collector_ips[i]
        var['collector%s' % i]['port'] = udp_port
        udp_port += 1
    collector_ports = var['ptf_test_indices'][0:2]
    setup_ptf(ptfhost, collector_ports)

    # -------- Testing ----------
    yield
    # -------- Teardown ----------
    config_reload(duthost, config_source='minigraph', wait=120)

# ----------------------------------------------------------------------------------


def setup_ptf(ptfhost, collector_ports):
    extra_vars = {'arp_responder_args': '--conf /tmp/sflow_arpresponder.conf'}
    ptfhost.host.options['variable_manager'].extra_vars.update(extra_vars)
    ptfhost.template(src="../ansible/roles/test/templates/arp_responder.conf.j2",
                     dest="/etc/supervisor/conf.d/arp_responder.conf")
    ptfhost.shell('supervisorctl reread')
    ptfhost.shell('supervisorctl update')
    for i in range(len(collector_ports)):
        ptfhost.shell('ifconfig eth%s %s/24' %
                      (collector_ports[i], var['collector%s' % i]['ip_addr']))
    ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")

# ----------------------------------------------------------------------------------


def config_dut_ports(duthost, ports, vlan):
    # https://github.com/sonic-net/sonic-buildimage/issues/2665
    # Introducing config vlan member add and remove for the test port due to above mentioned PR.
    # Even though port is deleted from vlan , the port shows its master as Bridge upon assigning ip address.
    # Hence config reload is done as workaround. ##FIXME
    for i in range(len(ports)):
        duthost.command('config vlan member del %s %s' % (vlan, ports[i]))
        duthost.command('config interface ip add %s %s/24' %
                        (ports[i], var['dut_intf_ips'][i]))
    duthost.command('config save -y')
    config_reload(duthost, config_source='config_db', wait=120)
    time.sleep(5)

# ----------------------------------------------------------------------------------


def get_ifindex(duthost, port):
    ifindex = duthost.shell('cat /sys/class/net/%s/ifindex' % port)['stdout']
    return ifindex

# ----------------------------------------------------------------------------------


def get_port_index(duthost, port):
    py_version = 'python' if '201911' in duthost.os_version else 'python3'

    # if sonic_py_common.port_util exist, use port_util from sonic_py_common.
    util_lib = "swsssdk"
    cmd = "{} -c \"import pkgutil; print(pkgutil.find_loader(\'sonic_py_common.port_util\'))\"".format(
        py_version)
    class_exist = duthost.shell(cmd)['stdout']
    if class_exist != "None":
        util_lib = "sonic_py_common"

    cmd = "{} -c \"from {} import port_util; print(port_util.get_index_from_str(\'{}\'))\""
    index = duthost.shell(cmd.format(py_version, util_lib, port))['stdout']
    return index

# ----------------------------------------------------------------------------------


@pytest.fixture
def config_sflow_agent(duthosts, rand_one_dut_hostname):
    # NOTE: When no agent-id is set, hsflowd chooses the agent-id based on simple heuristics
    # Hence, this fixture to keep the test stable
    duthost = duthosts[rand_one_dut_hostname]
    duthost.shell("config sflow agent-id del")  # Remove any existing agent-id
    duthost.shell("config sflow agent-id add Loopback0")
    yield
    duthost.shell("config sflow agent-id del")

# ----------------------------------------------------------------------------------


def config_sflow(duthost, sflow_status='enable'):
    duthost.shell('config sflow %s' % sflow_status)
    time.sleep(2)
# ----------------------------------------------------------------------------------

def config_sflow_direction(duthost,direction='rx'):
    duthost.shell('config sflow sample-direction %s' % direction)
    time.sleep(2)
# ----------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def config_sflow_feature(request, duthost):
    # Enable sFlow feature on DUT if enable_sflow_feature argument was passed
    if request.config.getoption("--enable_sflow_feature"):
        feature_status, _ = duthost.get_feature_status()
        if feature_status['sflow'] == 'disabled':
            duthost.shell("sudo config feature state sflow enabled")
            time.sleep(2)
# ----------------------------------------------------------------------------------

def check_egress_sflow_capability(duthost):
    data_list = duthost.shell('redis-cli -n 6 hgetall "SWITCH_CAPABILITY|switch"')['stdout_lines']
    switch_capability = {data_list[i]: data_list[i + 1] for i in range(0, len(data_list), 2)}
    if switch_capability.get('PORT_EGRESS_SAMPLE_CAPABLE'):
        return True
# ----------------------------------------------------------------------------------
  
def config_sflow_interfaces(duthost, intf, **kwargs):

    if 'status' in kwargs:
        duthost.shell('config sflow interface %s %s' %
                      (kwargs['status'], intf))
    if 'sample_rate' in kwargs:
        duthost.shell('config sflow interface sample-rate %s %s' %
                      (intf, kwargs['sample_rate']))
    if 'sample_direction' in kwargs:
        duthost.shell('config sflow interface sample-direction %s %s' %
                      (intf, kwargs['sample_direction']))

# ----------------------------------------------------------------------------------


def config_sflow_collector(duthost, collector, config):
    collector = var[collector]
    if config == 'add':
        duthost.shell('config sflow collector add %s %s --port %s ' %
                      (collector['name'], collector['ip_addr'], collector['port']))
    elif config == 'del':
        duthost.shell('config sflow collector  del %s' % collector['name'])
# ----------------------------------------------------------------------------------


def verify_show_sflow(duthost, status, **kwargs):
    show_sflow = duthost.shell('show sflow')['stdout']
    assert re.search(r"sFlow Admin State:\s+%s" %
                     status, show_sflow), "Sflow Admin State is not %s" % status
    if 'polling_int' in kwargs:
        assert re.search(r"sFlow Polling Interval:\s+%s" %
                         kwargs['polling_int'], show_sflow), "Sflow Polling Interval is not %s" % kwargs['polling_int']
    if 'agent_id' in kwargs:
        assert re.search(r"sFlow AgentID:\s+%s" %
                         kwargs['agent_id'], show_sflow), "Sflow Agent Id is not %s" % kwargs['agent_id']
    if 'sample_direction' in kwargs:
        assert re.search(r"sFlow Sample Direction:\s+%s" %
                         kwargs['sample_direction'], show_sflow), "Sflow SampleDirection is not %s" % kwargs['sample_direction']
    if 'collector' in kwargs:
        collector = kwargs['collector']
        if len(collector) is None:
            assert re.search("0 Collectors configured",
                             show_sflow), " Expected 0 collectors , but collectors are present"
        else:
            assert re.search("%s Collectors configured:" % len(
                collector), show_sflow), "Number of Sflow collectors should be %s" % len(collector)
            for col in collector:
                assert re.search(r"Name:\s+%s\s+IP addr:\s%s\s+UDP port:\s%s" % (
                    var[col]['name'], var[col]['ip_addr'], var[col]['port']), show_sflow),\
                    "col %s is not properly Configured" % col

# ----------------------------------------------------------------------------------


def verify_sflow_interfaces(duthost, intf, status, sampling_rate,sample_direction='rx'):
    show_sflow_intf = duthost.shell('show sflow interface')['stdout']
    assert re.search(r"%s\s+\|\s+%s\s+\|\s+%s\s+\|\s+%s" % (intf, status, sampling_rate,sample_direction),
                     show_sflow_intf), "Interface %s is not properly configured" % intf

# ----------------------------------------------------------------------------------

def verify_sflow_config_apply(duthost):
    sflow_sai_config_list = duthost.shell('redis-cli -n 1 keys *SAI_OBJECT_TYPE_SAMPLEPACKET*')['stdout_lines']
    for sflow_sai_config in sflow_sai_config_list:
        if 'SAI_OBJECT_TYPE_SAMPLEPACKET' in sflow_sai_config:
            return True
    return False

# ----------------------------------------------------------------------------------

@pytest.fixture
def partial_ptf_runner(request, ptfhost, tbinfo):
    def _partial_ptf_runner(**kwargs):
        params = {'testbed_type': tbinfo['topo']['name'],
                  'router_mac': var['router_mac'],
                  'dst_port': var['ptf_test_indices'][2],
                  'agent_id': var['lo_ip'],
                  'sflow_ports_file': "/tmp/sflow_ports.json"}
        params.update(kwargs)
        ptf_runner(host=ptfhost,
                   testdir="ptftests",
                   platform_dir="ptftests",
                   testname="sflow_test",
                   params=params,
                   socket_recv_size=16384,
                   log_file="/tmp/{}.{}.log".format(
                       request.cls.__name__, request.function.__name__),
                   is_python3=True)

    return _partial_ptf_runner

# ----------------------------------------------------------------------------------


@pytest.fixture(scope='class')
def sflowbase_config(duthosts, rand_one_dut_hostname):
    print("##### Executing sflowbase_config #####")
    duthost = duthosts[rand_one_dut_hostname]
    config_sflow(duthost, 'enable')
    config_sflow_direction(duthost,direction='both')
    config_sflow_collector(duthost, 'collector0', 'add')
    time.sleep(5)
    duthost.command("config sflow interface disable all")
    for port in var['sflow_ports']:
        config_sflow_interfaces(
            duthost, port, status='enable', sample_rate='512',sample_direction="both")
    time.sleep(2)
    verify_show_sflow(duthost, status='up', collector=[
                      'collector0'])
    time.sleep(120)
    for intf in var['sflow_ports']:
        verify_sflow_interfaces(duthost, intf, 'up', 512,sample_direction="both")


# ----------------------------------------------------------------------------------

class TestEgressSflow():
    """
    Test Egress Sflow functionality.
    Test port    -    Egress sflow is enabled on PortChannel104 member
    Traffic flow - Traffic ingresses on the members for PortChannel101,102 and 103 and 
                   is egress out of PortChannel104 member.
    Verification - As traffic is getting out from PortChannel104 member, sampling should happen here
                   and send the samples to the configured collector port and number of samples is 
                   expected to be in the expected range.
    """

    def test_egress_sflow_basic(self, duthosts, ptfhost, rand_one_dut_hostname, partial_ptf_runner):
        duthost = duthosts[rand_one_dut_hostname]
        # Enable sflow globally and enable sflow on 1 test interfaces
        # add single collector , send traffic and check samples are received in collector
        config_sflow(duthost, 'enable')
        config_sflow_direction(duthost,direction='tx')
        config_sflow_collector(duthost, 'collector0', 'add')
        time.sleep(5)
        duthost.command("config sflow interface disable all")
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='tx')
        verify_show_sflow(duthost, status='up', collector=['collector0'],sample_direction='tx')
        time.sleep(120)
        for intf in var['sflow_ports']:
            verify_sflow_interfaces(duthost, intf, 'up', 512,sample_direction='tx')
        #var['portmap'] = json.dumps(var['sflow_ports'])
        #ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")
        time.sleep(200)
        egress_port = list(var['egress_sflow_ports'].keys())
        partial_ptf_runner(
            enabled_sflow_interfaces=egress_port,egress_sflow_ports=egress_port,
            active_collectors="['collector0']",egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])

        
# ----------------------------------------------------------------------------------

@pytest.mark.usefixtures("sflowbase_config")
class TestEgressSflowEnableDisable():
    def test_InterfaceConfigDisableEnable(self, duthosts, rand_one_dut_hostname, partial_ptf_runner):
        """
        Disable Egress sflow and check that collector is not receiving samples.
        Enable Egress sflow and check that collector is receiving samples.
        """
        duthost = duthosts[rand_one_dut_hostname]
        #Disable egress sflow on the interface, by setting status = disable
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='disable', sample_rate='512',sample_direction='tx')
        time.sleep(200)
        egress_port = list(var['egress_sflow_ports'].keys())
        #import pdb; pdb.set_trace()
        partial_ptf_runner(
            enabled_sflow_interfaces=egress_port,egress_sflow_ports=egress_port,
            active_collectors="['collector0']",egress_sflow_enable=False,dst_ip=var['ipv4_peer_addrs'][-1])

        #Enable egress sflow on the interface
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='tx')
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=egress_port,egress_sflow_ports=egress_port,
            active_collectors="['collector0']",egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1])

        #Disable egress-sflow by changing the sample-direction to rx on the sflow interface
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='rx')
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),egress_sflow_ports=[],
            active_collectors="['collector0']",dst_ip=var['ipv4_peer_addrs'][-1])
             
        #Enable back the the egress sflow by setting sample-direction to tx
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='tx')
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=egress_port,egress_sflow_ports=egress_port,
            active_collectors="['collector0']",egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1])

# ----------------------------------------------------------------------------------

@pytest.mark.usefixtures("sflowbase_config")
class TestIngressEgressSflowSameInterface():
    def test_sflowSameIntf(self, duthosts, rand_one_dut_hostname, partial_ptf_runner):
        """
        Enable Ingress and Egress sflow on same interface
        """
        duthost = duthosts[rand_one_dut_hostname]
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='both')
        time.sleep(200)
        egress_port = list(var['egress_sflow_ports'].keys())
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_ports=egress_port,egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])

        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']")

        #Disable egress sflow at interface level and check only ingress sampling is happening
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='512',sample_direction='rx')
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_enable=False)


# ----------------------------------------------------------------------------------

@pytest.mark.usefixtures("sflowbase_config")
class TestIngressEgressSflowDiffInterface():
    def test_ingress_egress_sflow(self, duthosts, rand_one_dut_hostname, ptfhost,partial_ptf_runner):
        """
        Enable Ingress and Egress sflow on different Interface
        with different sampleRate at ingress and egress
        """
        duthost = duthosts[rand_one_dut_hostname]
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate='256',sample_direction='both')
            var['sflow_ports'][port]['sample_rate'] = 256

        #Testing with different sample-rate for egress sflow
        for port in var['egress_sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate = '1024')
            var['sflow_ports'][port]['sample_rate'] = 1024

        var['portmap'] = json.dumps(var['sflow_ports'])   
        ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_ports=list(var['egress_sflow_ports']),egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])

# ----------------------------------------------------------------------------------

@pytest.mark.usefixtures("sflowbase_config")
class TestMaxSampleRateConfig():
    def test_max_sampleRate_configOnly(self, duthosts, rand_one_dut_hostname):
        """
        Test max sflow sample rate configuration only
        Traffic test for this cannot be done using PTF infra,as PTF I/O infra takes longer time for sending higher traffic rate.
        """

        MAX_SAMPLE_RATE = 8388608
        duthost = duthosts[rand_one_dut_hostname]
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable', sample_rate=8388608,sample_direction='both')
        for port in var['sflow_ports']:
            verify_sflow_interfaces(duthost, port, 'up', 8388608,sample_direction='both')

        #Change the sample-direction and check there is not impact on max sample-rate configuration    
        #sample-direction to tx
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable',sample_direction='tx')
        for port in var['sflow_ports']:
            verify_sflow_interfaces(duthost, port, 'up', 8388608,sample_direction='tx')

        #sample-direction to rx
        for port in var['sflow_ports']:
            config_sflow_interfaces(
                duthost, port, status='enable',sample_direction='rx')
        for port in var['sflow_ports']:
            verify_sflow_interfaces(duthost, port, 'up', 8388608,sample_direction='rx')

# ------------------------------------------------------------------------------

@pytest.mark.disable_loganalyzer
@pytest.mark.usefixtures("sflowbase_config")
class TestReboot():

    def testRebootEgressSflowEnable(self, sflowbase_config, config_sflow_agent, duthost,
                              localhost, partial_ptf_runner, ptfhost):
        duthost.command("config sflow polling-interval 80")
        verify_show_sflow(duthost, status='up', polling_int=80)
        duthost.command('sudo config save -y')
        reboot(duthost, localhost)
        assert wait_until(
            300, 20, 0, duthost.critical_services_fully_started), "Not all critical services are fully started"
        assert wait_until(60, 5, 0, verify_sflow_config_apply, duthost)
        verify_show_sflow(duthost, status='up', collector=[
                          'collector0'], polling_int=80,sample_direction='both')
        for intf in var['sflow_ports']:
            var['sflow_ports'][intf]['ifindex'] = get_ifindex(duthost, intf)
            var['sflow_ports'][intf]['port_index'] = get_port_index(
                duthost, intf)
            verify_sflow_interfaces(duthost, intf, 'up', 512,sample_direction="both")
        var['portmap'] = json.dumps(var['sflow_ports'])
        ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_ports=list(var['egress_sflow_ports']),egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])


    def testWarmreboot(self, sflowbase_config, duthost, localhost, partial_ptf_runner, ptfhost):
        duthost.command('sudo config save -y')
        reboot(duthost, localhost, reboot_type='warm')
        assert wait_until(
            300, 20, 0, duthost.critical_services_fully_started), "Not all critical services are fully started"
        verify_show_sflow(duthost, status='up', collector=[
                          'collector0'],sample_direction='both')
        for intf in var['sflow_ports']:
            var['sflow_ports'][intf]['ifindex'] = get_ifindex(duthost, intf)
            var['sflow_ports'][intf]['port_index'] = get_port_index(
                duthost, intf)
            time.sleep(120)
            verify_sflow_interfaces(duthost, intf, 'up', 512,sample_direction="both")
        var['portmap'] = json.dumps(var['sflow_ports'])
        ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_ports=list(var['egress_sflow_ports']),egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])

    def testFastreboot(self, sflowbase_config, duthost, localhost, partial_ptf_runner, ptfhost):
        duthost.command('sudo config save -y')
        reboot(duthost, localhost, reboot_type='fast')
        assert wait_until(
            300, 20, 0, duthost.critical_services_fully_started), "Not all critical services are fully started"
        verify_show_sflow(duthost, status='up', collector=[
                          'collector0'],sample_direction='both')
        for intf in var['sflow_ports']:
            var['sflow_ports'][intf]['ifindex'] = get_ifindex(duthost, intf)
            var['sflow_ports'][intf]['port_index'] = get_port_index(
                duthost, intf)
            verify_sflow_interfaces(duthost, intf, 'up', 512,sample_direction="both")
        var['portmap'] = json.dumps(var['sflow_ports'])
        ptfhost.copy(content=var['portmap'], dest="/tmp/sflow_ports.json")
        time.sleep(200)
        partial_ptf_runner(
            enabled_sflow_interfaces=list(var['sflow_ports'].keys()),
            active_collectors="['collector0']",egress_sflow_ports=list(var['egress_sflow_ports']),egress_sflow_enable=True,dst_ip=var['ipv4_peer_addrs'][-1],asic_type=duthost.facts["asic_type"])

# ----------------------------------------------------------------------------------
