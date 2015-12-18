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
import re
import requests

from openerp.osv import orm, fields

from .runbot_repo import get_gitlab_params

_logger = logging.getLogger(__name__)

class RunbotBranch(orm.Model):
    _inherit = "runbot.branch"

    def _get_branch_url(self, cr, uid, ids, field_name, arg, context=None):
        """For gitlab branches get gitlab MR formatted branches
        """
        r = {}
        other_branch_ids = []
        for branch in self.browse(cr, uid, ids, context=context):
            if branch.repo_id.uses_gitlab and re.match('^[0-9]+$', branch.branch_name):
                r[branch.id] = "https://%s/merge_requests/%s" % (
                    branch.repo_id.base,
                    branch.branch_name,
                )
            else:
                other_branch_ids.append(branch.id)
        r.update(
            super(RunbotBranch, self)._get_branch_url(
                cr, uid, other_branch_ids, field_name, arg, context=context
            )
        )
        return r

    _columns = {
        'branch_url': fields.function(_get_branch_url, type='char', string='Branch url', readonly=1),
    }

    def _get_pull_info(self, cr, uid, ids, context=None):
        assert len(ids) == 1
        branch = self.browse(cr, uid, ids[0], context=context)
        repo = branch.repo_id

        if not repo.uses_gitlab:
            return super(RunbotBranch, self)._get_pull_info(cr, uid, ids, context=context)

        if repo.token and branch.name.startswith('refs/pull/'):
            pull_number = branch.name[len('refs/pull/'):]
            domain, name = get_gitlab_params(repo.base)

            url = "%s/api/v3/projects/%s/merge_requests" % (domain, repo.gitlab_project_id)
            r = requests.get(url, params={'private_token': repo.token, 'iid': pull_number})
            r.raise_for_status()
            data = r.json()
            if data:
                return {
                    'base': {
                        'ref': data[0]['target_branch'],
                    },
                    'head': {
                        'ref': data[0]['source_branch']
                    },
                    'state': data[0]['state']
                }
        return {}
