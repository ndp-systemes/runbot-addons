# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 NDP Systèmes (<http://www.ndp-systemes.fr>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import logging
import requests

from openerp import api, fields, models
from .runbot_repo import get_gitlab_params

_logger = logging.getLogger(__name__)


class RunbotBuild(models.Model):
    _inherit = "runbot.build"

    build_id = fields.Char(string="CI build ID")

    @api.multi
    def set_gitlab_commit_status(self, status, description=""):
        """Updates the status of the gitlab commit

        :param status: The status to set for this build.
        Can be: pending, running, success, failed, canceled
        :param description: An optional description for this build.
        """
        runbot_domain = self.env['runbot.repo'].domain()
        for build in self:
            repo = build.repo_id
            domain, name = get_gitlab_params(repo.base)
            url = "%s/api/v3/projects/%s/statuses/%s" % (domain, repo.gitlab_project_id, build.name)
            r = requests.post(
                url,
                data={
                    'private_token': repo.token,
                    'state': status,
                    # 'ref': repo.branch_id.branch_name,
                    'name': "ci/runbot",
                    'target_url': "http://%s/runbot/build/%s" % (runbot_domain, build.id),
                    'description': description,
                }
            )
            r.raise_for_status()

    @api.multi
    def github_status(self):
        """Notify gitlab of failed/successful builds"""
        for build in self:
            if build.repo_id.uses_gitlab:
                desc = "runbot build %s" % (build.dest,)
                if build.state == 'pending':
                    state = 'pending'
                elif build.state == 'testing':
                    state = 'running'
                elif build.state in ('running', 'done'):
                    if build.result == 'ok':
                        state = 'success'
                    else:
                        state = 'failed'
                    desc += " (runtime %ss)" % (build.job_time,)
                else:
                    continue
                _logger.debug("Updating gitlab status %s to %s", build.name, state)
                build.set_gitlab_commit_status(state, desc)
            else:
                super(RunbotBuild, build).github_status()
