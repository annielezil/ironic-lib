# vim: tabstop=4 shiftwidth=4 softtabstop=4
# coding=utf-8

# Copyright 2012 Hewlett-Packard Development Company, L.P.
# Copyright (c) 2012 NTT DOCOMO, INC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Baremetal IPMI power manager.
"""

import os
import stat
import tempfile

from oslo.config import cfg

from ironic.common import exception
from ironic.common import paths
from ironic.common import states
from ironic.common import utils
from ironic.manager import base
from ironic.openstack.common import log as logging
from ironic.openstack.common import loopingcall

opts = [
    cfg.StrOpt('terminal',
               default='shellinaboxd',
               help='path to baremetal terminal program'),
    cfg.StrOpt('terminal_cert_dir',
               default=None,
               help='path to baremetal terminal SSL cert(PEM)'),
    cfg.StrOpt('terminal_pid_dir',
               default=paths.state_path_def('baremetal/console'),
               help='path to directory stores pidfiles of baremetal_terminal'),
    cfg.IntOpt('ipmi_power_retry',
               default=5,
               help='maximal number of retries for IPMI operations'),
    ]

CONF = cfg.CONF
CONF.register_opts(opts)

LOG = logging.getLogger(__name__)


def _make_password_file(password):
    fd, path = tempfile.mkstemp()
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        f.write(password)
    return path


def _get_console_pid_path(node_id):
    name = "%s.pid" % node_id
    path = os.path.join(CONF.terminal_pid_dir, name)
    return path


def _get_console_pid(node_id):
    pid_path = _get_console_pid_path(node_id)
    if os.path.exists(pid_path):
        with open(pid_path, 'r') as f:
            pid_str = f.read()
        try:
            return int(pid_str)
        except ValueError:
            LOG.warn(_("pid file %s does not contain any pid"), pid_path)
    return None


class IPMI(base.PowerManager):
    """IPMI Power Driver for Baremetal Nova Compute

    This PowerManager class provides mechanism for controlling the power state
    of physical hardware via IPMI calls. It also provides serial console access
    where available.

    """

    def __init__(self, node, **kwargs):
        self.state = None
        self.retries = None
        self.node_id = node['id']
        self.address = node['pm_address']
        self.user = node['pm_user']
        self.password = node['pm_password']
        self.port = node['terminal_port']

        if self.node_id is None:
            raise exception.InvalidParameterValue(_("Node id not supplied "
                "to IPMI"))
        if self.address is None:
            raise exception.InvalidParameterValue(_("Address not supplied "
                "to IPMI"))
        if self.user is None:
            raise exception.InvalidParameterValue(_("User not supplied "
                "to IPMI"))
        if self.password is None:
            raise exception.InvalidParameterValue(_("Password not supplied "
                "to IPMI"))

    def _exec_ipmitool(self, command):
        args = ['ipmitool',
                '-I',
                'lanplus',
                '-H',
                self.address,
                '-U',
                self.user,
                '-f']
        pwfile = _make_password_file(self.password)
        try:
            args.append(pwfile)
            args.extend(command.split(" "))
            out, err = utils.execute(*args, attempts=3)
            LOG.debug(_("ipmitool stdout: '%(out)s', stderr: '%(err)s'"),
                      locals())
            return out, err
        finally:
            utils.delete_if_exists(pwfile)

    def _is_power(self, state):
        out_err = self._exec_ipmitool("power status")
        return out_err[0] == ("Chassis Power is %s\n" % state)

    def _power_on(self):
        """Turn the power to this node ON."""

        def _wait_for_power_on():
            """Called at an interval until the node's power is on."""

            if self._is_power("on"):
                self.state = states.ACTIVE
                raise loopingcall.LoopingCallDone()
            if self.retries > CONF.ipmi_power_retry:
                self.state = states.ERROR
                raise loopingcall.LoopingCallDone()
            try:
                self.retries += 1
                self._exec_ipmitool("power on")
            except Exception:
                LOG.exception(_("IPMI power on failed"))

        self.retries = 0
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_power_on)
        timer.start(interval=0.5).wait()

    def _power_off(self):
        """Turn the power to this node OFF."""

        def _wait_for_power_off():
            """Called at an interval until the node's power is off."""

            if self._is_power("off"):
                self.state = states.DELETED
                raise loopingcall.LoopingCallDone()
            if self.retries > CONF.ipmi_power_retry:
                self.state = states.ERROR
                raise loopingcall.LoopingCallDone()
            try:
                self.retries += 1
                self._exec_ipmitool("power off")
            except Exception:
                LOG.exception(_("IPMI power off failed"))

        self.retries = 0
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_power_off)
        timer.start(interval=0.5).wait()

    def _set_pxe_for_next_boot(self):
        try:
            self._exec_ipmitool("chassis bootdev pxe")
        except Exception:
            LOG.exception(_("IPMI set next bootdev failed"))

    def activate_node(self):
        """Turns the power to node ON."""
        if self._is_power("on") and self.state == states.ACTIVE:
            LOG.warning(_("Activate node called, but node %s "
                          "is already active") % self.address)
        self._set_pxe_for_next_boot()
        self._power_on()
        return self.state

    def reboot_node(self):
        """Cycles the power to a node."""
        self._power_off()
        self._set_pxe_for_next_boot()
        self._power_on()
        return self.state

    def deactivate_node(self):
        """Turns the power to node OFF, regardless of current state."""
        self._power_off()
        return self.state

    def is_power_on(self):
        return self._is_power("on")

    def start_console(self):
        if not self.port:
            return
        args = []
        args.append(CONF.terminal)
        if CONF.terminal_cert_dir:
            args.append("-c")
            args.append(CONF.terminal_cert_dir)
        else:
            args.append("-t")
        args.append("-p")
        args.append(str(self.port))
        args.append("--background=%s" % _get_console_pid_path(self.node_id))
        args.append("-s")

        try:
            pwfile = _make_password_file(self.password)
            ipmi_args = "/:%(uid)s:%(gid)s:HOME:ipmitool -H %(address)s" \
                    " -I lanplus -U %(user)s -f %(pwfile)s sol activate" \
                    % {'uid': os.getuid(),
                       'gid': os.getgid(),
                       'address': self.address,
                       'user': self.user,
                       'pwfile': pwfile,
                       }

            args.append(ipmi_args)
            # Run shellinaboxd without pipes. Otherwise utils.execute() waits
            # infinitely since shellinaboxd does not close passed fds.
            x = ["'" + arg.replace("'", "'\\''") + "'" for arg in args]
            x.append('</dev/null')
            x.append('>/dev/null')
            x.append('2>&1')
            utils.execute(' '.join(x), shell=True)
        finally:
            utils.delete_if_exists(pwfile)

    def stop_console(self):
        console_pid = _get_console_pid(self.node_id)
        if console_pid:
            # Allow exitcode 99 (RC_UNAUTHORIZED)
            utils.execute('kill', '-TERM', str(console_pid),
                          run_as_root=True,
                          check_exit_code=[0, 99])
        utils.delete_if_exists(_get_console_pid_path(self.node_id))
