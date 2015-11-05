# Import python libs
from __future__ import absolute_import
import os
import logging
import socket
import pprint

# Import libcloud
try:
    from libcloud.compute.base import NodeState
    from libcloud.compute.base import NodeAuthPassword
    HAS_LIBCLOUD = True
except ImportError:
    HAS_LIBCLOUD = False

# Import generic libcloud functions
from salt.cloud.libcloudfuncs import *   
# Import salt libs
import salt.utils

# Import salt.cloud libs
import salt.utils.cloud
import salt.utils.pycrypto as sup
import salt.config as config
from salt.utils import namespaced_function
from salt.exceptions import (
    SaltCloudConfigError,
    SaltCloudNotFound,
    SaltCloudSystemExit,
    SaltCloudExecutionFailure,
    SaltCloudExecutionTimeout
)

# Import netaddr IP matching
try:
    from netaddr import all_matching_cidrs
    HAS_NETADDR = True
except ImportError:
    HAS_NETADDR = False

# Get logging started
log = logging.getLogger(__name__)

__virtualname__ = 'dimensiondata'


# Some of the libcloud functions need to be in the same namespace as the
# functions defined in the module, so we create new function objects inside
# this module namespace
get_size = namespaced_function(get_size, globals())
get_image = namespaced_function(get_image, globals())
avail_locations = namespaced_function(avail_locations, globals())
avail_images = namespaced_function(avail_images, globals())
avail_sizes = namespaced_function(avail_sizes, globals())
script = namespaced_function(script, globals())
destroy = namespaced_function(destroy, globals())
reboot = namespaced_function(reboot, globals())
list_nodes = namespaced_function(list_nodes, globals())
list_nodes_full = namespaced_function(list_nodes_full, globals())
list_nodes_select = namespaced_function(list_nodes_select, globals())
show_instance = namespaced_function(show_instance, globals())
get_node = namespaced_function(get_node, globals())

# Only load in this module is the DIMENSIONDATA configurations are in place
def __virtual__():
    '''
    Set up the libcloud functions and check for DIMENSIONDATA configurations
    '''
    if get_configured_provider() is False:
        return False

    if get_dependencies() is False:
        return False

    return __virtualname__


def get_configured_provider():
    '''
    Return the first configured instance.
    '''
    return config.is_provider_configured(
        __opts__,
        __active_provider_name__ or __virtualname__,
        ('user_id','key', 'region')
    )


def get_dependencies():
    '''
    Warn if dependencies aren't met.
    '''
    deps = {
        'libcloud': HAS_LIBCLOUD,
        'netaddr': HAS_NETADDR
    }
    return config.check_driver_dependencies(
        __virtualname__,
        deps
    )

def create(vm_):
    '''
    Create a single VM from a data dict
    '''
    try:
        # Check for required profile parameters before sending any API calls.
        if vm_['profile'] and config.is_profile_configured(__opts__,
                                                           __active_provider_name__ or 'dimensiondata',
                                                           vm_['profile']) is False:
            return False
    except AttributeError:
        pass

    # Since using "provider: <provider-engine>" is deprecated, alias provider
    # to use driver: "driver: <provider-engine>"
    if 'provider' in vm_:
        vm_['driver'] = vm_.pop('provider')

    salt.utils.cloud.fire_event(
        'event',
        'starting create',
        'salt/cloud/{0}/creating'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )
    
  
   
    log.info('Creating Cloud VM {0}'.format(vm_['name']))
    conn = get_conn()
    rootPw = NodeAuthPassword(vm_['auth'])

    try:
        location = conn.ex_get_location_by_id(vm_['location'])
        images = conn.list_images(location=location)
        image = [x for x in images if x.id == vm_['image']][0]
        networks = conn.ex_list_network_domains(location=location)
        network_domain = [y for y in networks if y.name == vm_['network_domain']][0]
        vlan = conn.ex_list_vlans(location=location, network_domain=network_domain)[0]
        kwargs = {
        	'name': vm_['name'],
        	'image': image,
        	'auth': rootPw,
        	'ex_description': vm_['description'],
        	'ex_network_domain': network_domain,
        	'ex_vlan': vlan,
        	'ex_is_started': vm_['is_started']
	}
        '''
        salt.utils.cloud.fire_event(
        	'event',
        	'requesting instance',
        	'salt/cloud/{0}/requesting'.format(vm_['name']),
        	{'kwargs': {'name': kwargs['name'],
                    'image': kwargs['image'],
                   #'size': kwargs['size'],
                    'auth': '',
                    'ex_description': kwargs['ex_description'],
                    'ex_network_domain': kwargs['ex_network_domain'],
                    'ex_vlan': kwargs['ex_vlan'],
                    'ex_is_started': kwargs['ex_is_started']}},
        	    transport=__opts__['transport']
    	)
        '''
        data = conn.create_node(**kwargs)
    except Exception as exc:
        log.error(
            'Error creating {0} on DIMENSIONDATA\n\n'
            'The following exception was thrown by libcloud when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_['name'], exc
            ),
            # Show the traceback if the debug logging level is enabled
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    def __query_node_data(vm_, data):
        running = False
        try:
            node = show_instance(vm_['name'], 'action')
            running = (node['state'] == NodeState.RUNNING)
            log.debug(
                'Loaded node data for {0}:\nname: {1}\nstate: {2}'.format(
                    vm_['name'],
                    pprint.pformat(node['name']),
                    node['state']
                )
            )
        except Exception as err:
            log.error(
                'Failed to get nodes list: {0}'.format(
                    err
                ),
                # Show the traceback if the debug logging level is enabled
                exc_info_on_loglevel=logging.DEBUG
            )
            # Trigger a failure in the wait for IP function
            return False

        if not running:
            # Still not running, trigger another iteration
            return

        private = node['private_ips']
        public = node['public_ips']

        if private and not public:
            log.warn(
                'Private IPs returned, but not public... Checking for '
                'misidentified IPs'
            )
            for private_ip in private:
                private_ip = preferred_ip(vm_, [private_ip])
                if salt.utils.cloud.is_public_ip(private_ip):
                    log.warn('{0} is a public IP'.format(private_ip))
                    data.public_ips.append(private_ip)
                else:
                    log.warn('{0} is a private IP'.format(private_ip))
                    if private_ip not in data.private_ips:
                        data.private_ips.append(private_ip)

            if ssh_interface(vm_) == 'private_ips' and data.private_ips:
                return data

        if private:
            data.private_ips = private
            if ssh_interface(vm_) == 'private_ips':
                return data

        if public:
            data.public_ips = public
            if ssh_interface(vm_) != 'private_ips':
                return data

        log.debug('DATA')
        log.debug(data)

    try:
        data = salt.utils.cloud.wait_for_ip(
            __query_node_data,
            update_args=(vm_, data),
            timeout=config.get_cloud_config_value(
                'wait_for_ip_timeout', vm_, __opts__, default=25 * 60),
            interval=config.get_cloud_config_value(
                'wait_for_ip_interval', vm_, __opts__, default=30),
            max_failures=config.get_cloud_config_value(
                'wait_for_ip_max_failures', vm_, __opts__, default=60),
        )
    except (SaltCloudExecutionTimeout, SaltCloudExecutionFailure) as exc:
        try:
            # It might be already up, let's destroy it!
            destroy(vm_['name'])
        except SaltCloudSystemExit:
            pass
        finally:
            raise SaltCloudSystemExit(str(exc))

    log.debug('VM is now running')
    if ssh_interface(vm_) == 'private_ips':
        ip_address = preferred_ip(vm_, data.private_ips)
    else:
        ip_address = preferred_ip(vm_, data.public_ips)
    log.debug('Using IP address {0}'.format(ip_address))

    if salt.utils.cloud.get_salt_interface(vm_, __opts__) == 'private_ips':
        salt_ip_address = preferred_ip(vm_, data.private_ips)
        log.info('Salt interface set to: {0}'.format(salt_ip_address))
    else:
        salt_ip_address = preferred_ip(vm_, data.public_ips)
        log.debug('Salt interface set to: {0}'.format(salt_ip_address))

    if not ip_address:
        raise SaltCloudSystemExit(
            'No IP addresses could be found.'
        )

    vm_['salt_host'] = salt_ip_address
    vm_['ssh_host'] = ip_address
    vm_['password'] = vm_['auth']

    ret = salt.utils.cloud.bootstrap(vm_, __opts__)

    ret.update(data.__dict__)

    if 'password' in data.extra:
        del data.extra['password']

    log.info('Created Cloud VM \'{0[name]}\''.format(vm_))
    log.debug(
        '\'{0[name]}\' VM creation details:\n{1}'.format(
            vm_, pprint.pformat(data.__dict__)
        )
    )

    salt.utils.cloud.fire_event(
        'event',
        'created instance',
        'salt/cloud/{0}/created'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    return ret

def preferred_ip(vm_, ips):
    '''
    Return the preferred Internet protocol. Either 'ipv4' (default) or 'ipv6'.
    '''
    proto = config.get_cloud_config_value(
        'protocol', vm_, __opts__, default='ipv4', search_global=False
    )
    family = socket.AF_INET
    if proto == 'ipv6':
        family = socket.AF_INET6
    for ip in ips:
        try:
            socket.inet_pton(family, ip)
            return ip
        except Exception:
            continue
    return False


def ssh_interface(vm_):
    '''
    Return the ssh_interface type to connect to. Either 'public_ips' (default)
    or 'private_ips'.
    '''
    return config.get_cloud_config_value(
        'ssh_interface', vm_, __opts__, default='public_ips',
        search_global=False
    )

'''
NOT USED
def create(vm_):
    
    Create a single MCP VM.
   
    try:
        # Check for required profile parameters before sending any API calls.
        if vm_['profile'] and config.is_profile_configured(__opts__,
                                                           __active_provider_name__ or 'dimensiondata',
                                                           vm_['profile']) is False:
            return False
    except AttributeError:
        pass

    # Since using "provider: <provider-engine>" is deprecated, alias provider
    # to use driver: "driver: <provider-engine>"
    if 'provider' in vm_:
        vm_['driver'] = vm_.pop('provider')

    #if _validate_name(vm_['name']) is False:
     #   return False

    salt.utils.cloud.fire_event(
        'event',
        'starting create',
        'salt/cloud/{0}/creating'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    log.info('Creating Cloud VM {0}'.format(vm_['name']))

    
    # Bootstrap!
    ret = salt.utils.cloud.bootstrap(vm_, __opts__)
     
    return ret
'''
def get_conn():
   '''
   Return a conn object for the passed VM data
   '''
   
   vm_ = get_configured_provider()
   driver = get_driver(Provider.DIMENSIONDATA)
 
   region = config.get_cloud_config_value(
        'region', vm_, __opts__, search_global=False
   )   
    
   import libcloud.security
   libcloud.security.VERIFY_SSL_CERT = False

   user_id = config.get_cloud_config_value(
       'user_id', vm_, __opts__, search_global=False
   )
   key = config.get_cloud_config_value(
       'key', vm_, __opts__, search_global=False
   )

   if key is not None:
       log.debug('DimensionData authenticating using password')

   return driver(
            user_id,
            key,
            region
       )