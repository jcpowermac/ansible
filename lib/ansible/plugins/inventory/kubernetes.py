#!/usr/bin/env python
#
# Based on docker dynamic inventory script
#
# (c) 2017 Joseph Callen <jcallen@redhat.com>
#
# This file is part of Ansible.
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#

'''
DOCUMENTATION:

Kubernetes Inventory Script
=======================

Using the kubernetes API this inventory script generates dynamic inventory of pods, namespaces and services.

Requirements
------------

Using this dynamic inventory script requires having the kubernetes module
<https://github.com/kubernetes-incubator/client-python> installed on the host running Ansible. To install kubernetes:

   pip install kubernetes

Groups
------
Container instances are grouped by:
    - namespace
    - pod

Configuration File
------------------
plugin:
    description: With two available plugins this option allows the use of both either 'kubernetes' or 'kubectl'
    required: True
contexts:
    description: list of context dictionaries
    required: True

    name:
        description: (string) The name of the context (not currently used)
        required: True
    server:
        description: (string) This is required if kubernetes_config_file or local_kube_config is not used
        required: False
    port:
        description: (int) The kubernetes api port.This is required if kubernetes_config_file or local_kube_config is not used
        required: False
    token:
        description: (string) Bearer Token. This is required if kubernetes_config_file or local_kube_config is not used
        required: False
    local_kube_config:
        description:
            - (boolean) If true use the local kubernetes config which defaults to ~/.kube.config.
            - Otherwise use kubernetes_config_file or server,port,username,token parameters
        required: True
    kubernetes_config_file:
        description: (string) Path and filename to a kubernetes config file
        required: False
    namespaces:
        description: The namespaces to query for available pods and services
        required: True

Examples
--------
# ansible-playbook -i ~/ansible/contrib/inventory/kubernetes.py site.yml

    - name: kube test
      hosts: kube-ansible
      gather_facts: True
      tasks:
        - name: raw_module
          raw: date

        - name: debug
          debug:
            msg: "{{ hostvars[inventory_hostname] }}"

        - name: command_module
          command: "echo hello"

        - name: Files
          file:
            path: "/opt/app-root"
            recurse: yes

        - name: find files
          find:
            path: "/opt/app-root"
            recurse: yes
'''

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import sys
import json
import argparse
import yaml

# NOTE: This was copied from inventory/docker.py
# Manipulation of the path is needed because the kubernetes
# module is imported by the name kubernetes, and because this file
# is also named kubernetes
for path in [os.getcwd(), '', os.path.dirname(os.path.abspath(__file__))]:
    try:
        del sys.path[sys.path.index(path)]
    except:
        pass
HAS_KUBERNETES = True

try:
    from kubernetes import config
    from kubernetes.config import ConfigException
    from kubernetes.config.kube_config import KubeConfigLoader
    from kubernetes.client import configuration
    from kubernetes.client.apis import core_v1_api
    from kubernetes.client.rest import ApiException
except ImportError:
    HAS_KUBERNETES = False
    pass


from ansible.errors import AnsibleParserError
from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.plugins.inventory import BaseInventoryPlugin

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display

    display = Display()


class InventoryModule(BaseInventoryPlugin):
    NAME = 'kubernetes'

    def _fail(self, msg):
        display.error(u"{0}".format(to_text(msg)))

    def _generate_kubernetes_config(self, context):
        username = context.get('username')
        server = "{0}:{1}".format(context.get('server'), context.get('port'))
        current_context = "{0}/{1}/{2}".format(
            context.get('namespaces')[0],
            server,
            username,
        )
        users_name = "{0}/{1}".format(username, server)

        kubernetes_config = {
            "current-context": current_context,
            "clusters": [
                {
                    "cluster": {
                        "insecure-skip-tls-verify": "true",
                        "server": "https://{0}".format(server)
                    },
                    "name": server
                }
            ],
            "contexts": [
                {
                    "context": {
                        "cluster": server,
                        "namespace": context.get('namespaces')[0],
                        "user": users_name
                    },
                    "name": current_context
                }
            ],
            "users": [
                {
                    "name": users_name,
                    "user": {
                        "token": context.get('token')
                    }
                }
            ]
        }
        return kubernetes_config, current_context

    def _connect(self, context):

        try:
            if context.get('local_kube_config'):
                config.load_kube_config()
            elif context.get('kubernetes_config_file'):
                config.load_kube_config(config_file=context.get('kubernetes_config_file'))
            else:
                kubernetes_config, active_context = self._generate_kubernetes_config(context)
                kube_loader = KubeConfigLoader(config_dict=kubernetes_config,
                                               active_context=active_context)
                kube_loader.load_and_set()

            self.api = core_v1_api.CoreV1Api()
        except ConfigException as e:
            self._fail(e.message)
        except ApiException as e:
            self._fail(e.message)
        except Exception as e:
            self._fail(e.message)


    def _kube_service_group(self, namespace):
        try:
            endpoints = self.api.list_namespaced_endpoints(namespace=namespace)
            for e in endpoints.items:
                service_name = e.metadata.name
                self.inventory.add_group(service_name)

                for s in e.subsets:
                    for a in s.addresses:
                        try:
                            if a.target_ref:
                                if "Pod" in a.target_ref.kind:
                                    self.inventory.add_child(service_name, a.target_ref.name)
                        except NameError:
                            pass
        except ConfigException as e:
            self._fail(e.message)
        except ApiException as e:
            self._fail(e.message)
        except Exception as e:
            self._fail(e.message)


    def _groupadd(self, group_name, child_name):
        self.inventory.add_group(group_name)
        self.inventory.add_child(group_name, child_name)

    def _hostvars(self, namespace, pod_name, container_name, context):

        # Ok what is going on here?
        # The ansible_host is the name of the pod.  The kube_container is the name of
        # the container running in the pod.  The issue is the inventory_hostname must
        # be unique.  When using deployments (or DeploymentConfigs in OpenShift) there can be more
        # than one replica pod.  The pod name will be unique but the container name will not.
        # So the inventory_hostname will be the concatenation of the pod and container name.


        self.inventory.add_host(self.inventory_hostname)
        self.inventory.set_variable(self.inventory_hostname, "ansible_host", pod_name)
        self.inventory.set_variable(self.inventory_hostname, "ansible_connection", self.plugin)
        self.inventory.set_variable(self.inventory_hostname, "ansible_user", context.get('username', None))
        self.inventory.set_variable(self.inventory_hostname, "ansible_password", context.get('token', None))

        self.inventory.set_variable(self.inventory_hostname, "ansible_kubernetes_port", context.get('port', 8443))
        self.inventory.set_variable(self.inventory_hostname, "ansible_kubernetes_cluster", context.get('server', None))

        # overlapping options with kubectl and kubernetes connection plugin.
        self.inventory.set_variable(self.inventory_hostname, "ansible_kube_namespace", namespace)
        self.inventory.set_variable(self.inventory_hostname, "ansible_kube_config_file", context.get('kubernetes_config_file', None))
        self.inventory.set_variable(self.inventory_hostname, "ansible_kube_container", container_name)

    def verify_file(self, path):
        valid = False
        if super(InventoryModule, self).verify_file(path):
            if path.endswith('.kube.yaml') or path.endswith('.kube.yml'):
                valid = True
        return valid

    def parse(self, inventory, loader, path, cache=True):
        # What if there was no configuration file?
        # Is this a possible configuration?
        super(InventoryModule, self).parse(inventory, loader, path)
        try:
            config_data = self.loader.load_from_file(path)
        except Exception as e:
            raise AnsibleParserError(e)

        self._populate(config_data)

    def _populate(self, config_data):

        try:
            self.plugin = config_data.get('plugin', 'kubernetes')
            for context in config_data.get('contexts'):
                self._connect(context)
                for namespace in context.get('namespaces'):
                    pods = self.api.list_namespaced_pod(namespace=namespace)
                    for pod in pods.items:
                        # OpenShift launches pods with the name -build
                        # when using a BuildConfig that creates an image.
                        # Do not include these pods in the host inventory
                        if "-build" not in pod.metadata.name:
                            for container in pod.spec.containers:
                                self.inventory_hostname = "{0}_{1}".format(container.name, pod.metadata.name)
                                self._hostvars(namespace, pod.metadata.name, container.name, context)
                                self._groupadd(namespace, self.inventory_hostname)
                                self._groupadd(pod.metadata.name, self.inventory_hostname)
                            for key, val in pod.metadata.labels.iteritems():
                                self._groupadd("{0}_{1}".format(key,val), pod.metadata.name)
                    self._kube_service_group(namespace)

        except ConfigException as e:
            self._fail(e.message)
        except ApiException as e:
            self._fail(e.message)
        except Exception as e:
            self._fail(e.message)
