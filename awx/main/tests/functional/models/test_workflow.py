# Python
import pytest
from unittest import mock
import json

# AWX
from awx.main.models.workflow import (
    WorkflowJob,
    WorkflowJobNode,
    WorkflowJobTemplateNode,
    WorkflowJobTemplate,
)
from awx.main.models.jobs import JobTemplate, Job
from awx.main.models.projects import ProjectUpdate
from awx.main.models.credential import Credential, CredentialType
from awx.main.models.label import Label
from awx.main.models.ha import InstanceGroup
from awx.main.scheduler.dag_workflow import WorkflowDAG
from awx.api.versioning import reverse
from awx.api.views import WorkflowJobTemplateNodeSuccessNodesList

# Django
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.utils.timezone import now


class TestWorkflowDAGFunctional(TransactionTestCase):
    def workflow_job(self, states=['new', 'new', 'new', 'new', 'new']):
        """
        Workflow topology:
               node[0]
                /\
              s/  \f
              /    \
           node[1] node[3]
             /       \
           s/         \f
           /           \
        node[2]       node[4]
        """
        wfj = WorkflowJob.objects.create()
        jt = JobTemplate.objects.create(name='test-jt')
        nodes = [WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=jt) for i in range(0, 5)]
        for node, state in zip(nodes, states):
            if state:
                node.job = jt.create_job()
                node.job.status = state
                node.job.save()
                node.save()
        nodes[0].success_nodes.add(nodes[1])
        nodes[1].success_nodes.add(nodes[2])
        nodes[0].failure_nodes.add(nodes[3])
        nodes[3].failure_nodes.add(nodes[4])
        return wfj

    def test_build_WFJT_dag(self):
        """
        Test that building the graph uses 4 queries
         1 to get the nodes
         3 to get the related success, failure, and always connections
        """
        dag = WorkflowDAG()
        wfj = self.workflow_job()
        with self.assertNumQueries(4):
            dag._init_graph(wfj)

    def test_workflow_done(self):
        wfj = self.workflow_job(states=['failed', None, None, 'successful', None])
        dag = WorkflowDAG(workflow_job=wfj)
        assert 3 == len(dag.mark_dnr_nodes())
        is_done = dag.is_workflow_done()
        has_failed, reason = dag.has_workflow_failed()
        self.assertTrue(is_done)
        self.assertFalse(has_failed)
        assert reason is None

        # verify that relaunched WFJ fails if a JT leaf is deleted
        for jt in JobTemplate.objects.all():
            jt.delete()
        relaunched = wfj.create_relaunch_workflow_job()
        dag = WorkflowDAG(workflow_job=relaunched)
        dag.mark_dnr_nodes()
        is_done = dag.is_workflow_done()
        has_failed, reason = dag.has_workflow_failed()
        self.assertTrue(is_done)
        self.assertTrue(has_failed)
        assert "Workflow job node {} related unified job template missing".format(wfj.workflow_nodes.all()[0].id)

    def test_workflow_fails_for_no_error_handler(self):
        wfj = self.workflow_job(states=['successful', 'failed', None, None, None])
        dag = WorkflowDAG(workflow_job=wfj)
        dag.mark_dnr_nodes()
        is_done = dag.is_workflow_done()
        has_failed = dag.has_workflow_failed()
        self.assertTrue(is_done)
        self.assertTrue(has_failed)

    def test_workflow_fails_leaf(self):
        wfj = self.workflow_job(states=['successful', 'successful', 'failed', None, None])
        dag = WorkflowDAG(workflow_job=wfj)
        dag.mark_dnr_nodes()
        is_done = dag.is_workflow_done()
        has_failed = dag.has_workflow_failed()
        self.assertTrue(is_done)
        self.assertTrue(has_failed)

    def test_workflow_not_finished(self):
        wfj = self.workflow_job(states=['new', None, None, None, None])
        dag = WorkflowDAG(workflow_job=wfj)
        dag.mark_dnr_nodes()
        is_done = dag.is_workflow_done()
        has_failed, reason = dag.has_workflow_failed()
        self.assertFalse(is_done)
        self.assertFalse(has_failed)
        assert reason is None


@pytest.mark.django_db
class TestWorkflowDNR:
    @pytest.fixture
    def workflow_job_fn(self):
        def fn(states=['new', 'new', 'new', 'new', 'new', 'new']):
            r"""
            Workflow topology:
                   node[0]
                    /   |
                  s     f
                  /     |
               node[1] node[3]
                 /      |
                s       f
               /        |
            node[2]    node[4]
               \        |
                s       f
                 \      |
                  node[5]
            """
            wfj = WorkflowJob.objects.create()
            jt = JobTemplate.objects.create(name='test-jt')
            nodes = [WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=jt) for i in range(0, 6)]
            for node, state in zip(nodes, states):
                if state:
                    node.job = jt.create_job()
                    node.job.status = state
                    node.job.save()
                    node.save()
            nodes[0].success_nodes.add(nodes[1])
            nodes[1].success_nodes.add(nodes[2])
            nodes[0].failure_nodes.add(nodes[3])
            nodes[3].failure_nodes.add(nodes[4])
            nodes[2].success_nodes.add(nodes[5])
            nodes[4].failure_nodes.add(nodes[5])
            return wfj, nodes

        return fn

    def test_workflow_dnr_because_parent(self, workflow_job_fn):
        wfj, nodes = workflow_job_fn(
            states=[
                'successful',
                None,
                None,
                None,
                None,
                None,
            ]
        )
        dag = WorkflowDAG(workflow_job=wfj)
        workflow_nodes = dag.mark_dnr_nodes()
        assert 2 == len(workflow_nodes)
        assert nodes[3] in workflow_nodes
        assert nodes[4] in workflow_nodes


@pytest.mark.django_db
class TestWorkflowJob:
    @pytest.fixture
    def workflow_job(self, workflow_job_template_factory):
        wfjt = workflow_job_template_factory('blah').workflow_job_template
        wfj = WorkflowJob.objects.create(workflow_job_template=wfjt)

        nodes = [WorkflowJobTemplateNode.objects.create(workflow_job_template=wfjt) for i in range(0, 5)]

        nodes[0].success_nodes.add(nodes[1])
        nodes[1].success_nodes.add(nodes[2])

        nodes[0].failure_nodes.add(nodes[3])
        nodes[3].failure_nodes.add(nodes[4])

        return wfj

    def test_inherit_job_template_workflow_nodes(self, mocker, workflow_job):
        workflow_job.copy_nodes_from_original(original=workflow_job.workflow_job_template)

        nodes = WorkflowJob.objects.get(id=workflow_job.id).workflow_job_nodes.all().order_by('created')
        assert nodes[0].success_nodes.filter(id=nodes[1].id).exists()
        assert nodes[1].success_nodes.filter(id=nodes[2].id).exists()
        assert nodes[0].failure_nodes.filter(id=nodes[3].id).exists()
        assert nodes[3].failure_nodes.filter(id=nodes[4].id).exists()

    def test_inherit_ancestor_artifacts_from_job(self, job_template, mocker):
        """
        Assure that nodes along the line of execution inherit artifacts
        from both jobs ran, and from the accumulation of old jobs
        """
        # Related resources
        wfj = WorkflowJob.objects.create(name='test-wf-job')
        job = Job.objects.create(name='test-job', artifacts={'b': 43})
        # Workflow job nodes
        job_node = WorkflowJobNode.objects.create(workflow_job=wfj, job=job, ancestor_artifacts={'a': 42})
        queued_node = WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=job_template)
        # Connect old job -> new job
        mocker.patch.object(queued_node, 'get_parent_nodes', lambda: [job_node])
        assert queued_node.get_job_kwargs()['extra_vars'] == {'a': 42, 'b': 43}
        assert queued_node.ancestor_artifacts == {'a': 42, 'b': 43}

    def test_inherit_ancestor_artifacts_from_project_update(self, project, job_template, mocker):
        """
        Test that the existence of a project update (no artifacts) does
        not break the flow of ancestor_artifacts
        """
        # Related resources
        wfj = WorkflowJob.objects.create(name='test-wf-job')
        update = ProjectUpdate.objects.create(name='test-update', project=project)
        # Workflow job nodes
        project_node = WorkflowJobNode.objects.create(workflow_job=wfj, job=update, ancestor_artifacts={'a': 42, 'b': 43})
        queued_node = WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=job_template)
        # Connect project update -> new job
        mocker.patch.object(queued_node, 'get_parent_nodes', lambda: [project_node])
        assert queued_node.get_job_kwargs()['extra_vars'] == {'a': 42, 'b': 43}
        assert queued_node.ancestor_artifacts == {'a': 42, 'b': 43}

    def test_combine_prompts_WFJT_to_node(self, project, inventory, organization):
        """
        Test that complex prompts like variables, credentials, labels, etc
        are properly combined from the workflow-level with the node-level
        """
        jt = JobTemplate.objects.create(
            project=project,
            inventory=inventory,
            ask_variables_on_launch=True,
            ask_credential_on_launch=True,
            ask_instance_groups_on_launch=True,
            ask_labels_on_launch=True,
            ask_limit_on_launch=True,
        )
        wj = WorkflowJob.objects.create(name='test-wf-job', extra_vars='{}')

        common_ig = InstanceGroup.objects.create(name='common')
        common_ct = CredentialType.objects.create(name='common')

        node = WorkflowJobNode.objects.create(workflow_job=wj, unified_job_template=jt, extra_vars={'node_key': 'node_val'})
        node.limit = 'node_limit'
        node.save()
        node_cred_unique = Credential.objects.create(credential_type=CredentialType.objects.create(name='node'))
        node_cred_conflicting = Credential.objects.create(credential_type=common_ct)
        node.credentials.add(node_cred_unique, node_cred_conflicting)
        node_labels = [Label.objects.create(name='node1', organization=organization), Label.objects.create(name='node2', organization=organization)]
        node.labels.add(*node_labels)
        node_igs = [common_ig, InstanceGroup.objects.create(name='node')]
        for ig in node_igs:
            node.instance_groups.add(ig)

        # assertions for where node has prompts but workflow job does not
        data = node.get_job_kwargs()
        assert data['extra_vars'] == {'node_key': 'node_val'}
        assert set(data['credentials']) == set([node_cred_conflicting, node_cred_unique])
        assert data['instance_groups'] == node_igs
        assert set(data['labels']) == set(node_labels)
        assert data['limit'] == 'node_limit'

        # add prompts to the WorkflowJob
        wj.limit = 'wj_limit'
        wj.extra_vars = {'wj_key': 'wj_val'}
        wj.save()
        wj_cred_unique = Credential.objects.create(credential_type=CredentialType.objects.create(name='wj'))
        wj_cred_conflicting = Credential.objects.create(credential_type=common_ct)
        wj.credentials.add(wj_cred_unique, wj_cred_conflicting)
        wj.labels.add(Label.objects.create(name='wj1', organization=organization), Label.objects.create(name='wj2', organization=organization))
        wj_igs = [InstanceGroup.objects.create(name='wj'), common_ig]
        for ig in wj_igs:
            wj.instance_groups.add(ig)

        # assertions for behavior where node and workflow jobs have prompts
        data = node.get_job_kwargs()
        assert data['extra_vars'] == {'node_key': 'node_val', 'wj_key': 'wj_val'}
        assert set(data['credentials']) == set([wj_cred_unique, wj_cred_conflicting, node_cred_unique])
        assert data['instance_groups'] == wj_igs
        assert set(data['labels']) == set(node_labels)  # as exception, WFJT labels not applied
        assert data['limit'] == 'wj_limit'


@pytest.mark.django_db
class TestWorkflowJobTemplate:
    @pytest.fixture
    def wfjt(self, workflow_job_template_factory, organization):
        wfjt = workflow_job_template_factory('test', organization=organization).workflow_job_template
        wfjt.organization = organization
        nodes = [WorkflowJobTemplateNode.objects.create(workflow_job_template=wfjt) for i in range(0, 3)]
        nodes[0].success_nodes.add(nodes[1])
        nodes[1].failure_nodes.add(nodes[2])
        return wfjt

    def test_node_parentage(self, wfjt):
        # test success parent
        wfjt_node = wfjt.workflow_job_template_nodes.all()[1]
        parent_qs = wfjt_node.get_parent_nodes()
        assert len(parent_qs) == 1
        assert parent_qs[0] == wfjt.workflow_job_template_nodes.all()[0]
        # test failure parent
        wfjt_node = wfjt.workflow_job_template_nodes.all()[2]
        parent_qs = wfjt_node.get_parent_nodes()
        assert len(parent_qs) == 1
        assert parent_qs[0] == wfjt.workflow_job_template_nodes.all()[1]

    def test_topology_validator(self, wfjt):
        test_view = WorkflowJobTemplateNodeSuccessNodesList()
        nodes = wfjt.workflow_job_template_nodes.all()
        # test cycle validation
        assert test_view.is_valid_relation(nodes[2], nodes[0]) == {'Error': 'Cycle detected.'}

    def test_always_success_failure_creation(self, wfjt, admin, get):
        wfjt_node = wfjt.workflow_job_template_nodes.all()[1]
        node = WorkflowJobTemplateNode.objects.create(workflow_job_template=wfjt)
        wfjt_node.always_nodes.add(node)
        assert len(node.get_parent_nodes()) == 1
        url = reverse('api:workflow_job_template_node_list') + str(wfjt_node.id) + '/'
        resp = get(url, admin)
        assert node.id in resp.data['always_nodes']

    def test_wfjt_unique_together_with_org(self, organization):
        wfjt1 = WorkflowJobTemplate(name='foo', organization=organization)
        wfjt1.save()
        wfjt2 = WorkflowJobTemplate(name='foo', organization=organization)
        with pytest.raises(ValidationError):
            wfjt2.validate_unique()
        wfjt2 = WorkflowJobTemplate(name='foo', organization=None)
        wfjt2.validate_unique()


@pytest.mark.django_db
class TestWorkflowJobTemplatePrompts:
    """These are tests for prompts that live on the workflow job template model
    not the node, prompts apply for entire workflow
    """

    @pytest.fixture
    def wfjt_prompts(self):
        return WorkflowJobTemplate.objects.create(
            ask_variables_on_launch=True,
            ask_inventory_on_launch=True,
            ask_tags_on_launch=True,
            ask_labels_on_launch=True,
            ask_limit_on_launch=True,
            ask_scm_branch_on_launch=True,
            ask_skip_tags_on_launch=True,
        )

    @pytest.fixture
    def prompts_data(self, inventory):
        return dict(
            inventory=inventory,
            extra_vars={'foo': 'bar'},
            limit='webservers',
            scm_branch='release-3.3',
            job_tags='foo',
            skip_tags='bar',
        )

    def test_apply_workflow_job_prompts(self, workflow_job_template, wfjt_prompts, prompts_data, inventory):
        # null or empty fields used
        workflow_job = workflow_job_template.create_unified_job()
        assert workflow_job.limit is None
        assert workflow_job.inventory is None
        assert workflow_job.scm_branch is None
        assert workflow_job.job_tags is None
        assert workflow_job.skip_tags is None
        assert len(workflow_job.labels.all()) == 0

        # fields from prompts used
        workflow_job = workflow_job_template.create_unified_job(**prompts_data)
        assert json.loads(workflow_job.extra_vars) == {'foo': 'bar'}
        assert workflow_job.limit == 'webservers'
        assert workflow_job.inventory == inventory
        assert workflow_job.scm_branch == 'release-3.3'
        assert workflow_job.job_tags == 'foo'
        assert workflow_job.skip_tags == 'bar'

        # non-null fields from WFJT used
        workflow_job_template.inventory = inventory
        workflow_job_template.limit = 'fooo'
        workflow_job_template.scm_branch = 'bar'
        workflow_job_template.job_tags = 'baz'
        workflow_job_template.skip_tags = 'dinosaur'
        workflow_job = workflow_job_template.create_unified_job()
        assert workflow_job.limit == 'fooo'
        assert workflow_job.inventory == inventory
        assert workflow_job.scm_branch == 'bar'
        assert workflow_job.job_tags == 'baz'
        assert workflow_job.skip_tags == 'dinosaur'

    @pytest.mark.django_db
    def test_process_workflow_job_prompts(self, inventory, workflow_job_template, wfjt_prompts, prompts_data):
        accepted, rejected, errors = workflow_job_template._accept_or_ignore_job_kwargs(**prompts_data)
        assert accepted == {}
        assert rejected == prompts_data
        assert errors
        accepted, rejected, errors = wfjt_prompts._accept_or_ignore_job_kwargs(**prompts_data)
        assert accepted == prompts_data
        assert rejected == {}
        assert not errors

    @pytest.mark.django_db
    def test_set_all_the_prompts(self, post, organization, inventory, org_admin):
        r = post(
            url=reverse('api:workflow_job_template_list'),
            data=dict(
                name='My new workflow',
                organization=organization.id,
                inventory=inventory.id,
                limit='foooo',
                ask_limit_on_launch=True,
                scm_branch='bar',
                ask_scm_branch_on_launch=True,
                job_tags='foo',
                skip_tags='bar',
            ),
            user=org_admin,
            expect=201,
        )
        wfjt = WorkflowJobTemplate.objects.get(id=r.data['id'])
        assert wfjt.char_prompts == {
            'limit': 'foooo',
            'scm_branch': 'bar',
            'job_tags': 'foo',
            'skip_tags': 'bar',
        }
        assert wfjt.ask_scm_branch_on_launch is True
        assert wfjt.ask_limit_on_launch is True

        launch_url = r.data['related']['launch']
        with mock.patch('awx.main.queue.CallbackQueueDispatcher.dispatch', lambda self, obj: None):
            r = post(url=launch_url, data=dict(scm_branch='prompt_branch', limit='prompt_limit'), user=org_admin, expect=201)
        assert r.data['limit'] == 'prompt_limit'
        assert r.data['scm_branch'] == 'prompt_branch'

    @pytest.mark.django_db
    def test_set_all_ask_for_prompts_false_from_post(self, post, organization, inventory, org_admin):
        '''
        Tests default behaviour and values of ask_for_* fields on WFJT via POST
        '''
        r = post(
            url=reverse('api:workflow_job_template_list'),
            data=dict(
                name='workflow that tests ask_for prompts',
                organization=organization.id,
                inventory=inventory.id,
                job_tags='',
                skip_tags='',
            ),
            user=org_admin,
            expect=201,
        )
        wfjt = WorkflowJobTemplate.objects.get(id=r.data['id'])

        assert wfjt.ask_inventory_on_launch is False
        assert wfjt.ask_labels_on_launch is False
        assert wfjt.ask_limit_on_launch is False
        assert wfjt.ask_scm_branch_on_launch is False
        assert wfjt.ask_skip_tags_on_launch is False
        assert wfjt.ask_tags_on_launch is False
        assert wfjt.ask_variables_on_launch is False

    @pytest.mark.django_db
    def test_set_all_ask_for_prompts_true_from_post(self, post, organization, inventory, org_admin):
        '''
        Tests behaviour and values of ask_for_* fields on WFJT via POST
        '''
        r = post(
            url=reverse('api:workflow_job_template_list'),
            data=dict(
                name='workflow that tests ask_for prompts',
                organization=organization.id,
                inventory=inventory.id,
                job_tags='',
                skip_tags='',
                ask_inventory_on_launch=True,
                ask_labels_on_launch=True,
                ask_limit_on_launch=True,
                ask_scm_branch_on_launch=True,
                ask_skip_tags_on_launch=True,
                ask_tags_on_launch=True,
                ask_variables_on_launch=True,
            ),
            user=org_admin,
            expect=201,
        )
        wfjt = WorkflowJobTemplate.objects.get(id=r.data['id'])

        assert wfjt.ask_inventory_on_launch is True
        assert wfjt.ask_labels_on_launch is True
        assert wfjt.ask_limit_on_launch is True
        assert wfjt.ask_scm_branch_on_launch is True
        assert wfjt.ask_skip_tags_on_launch is True
        assert wfjt.ask_tags_on_launch is True
        assert wfjt.ask_variables_on_launch is True


@pytest.mark.django_db
def test_workflow_ancestors(organization):
    # Spawn order of templates grandparent -> parent -> child
    # create child WFJT and workflow job
    child = WorkflowJobTemplate.objects.create(organization=organization, name='child')
    child_job = WorkflowJob.objects.create(workflow_job_template=child, launch_type='workflow')
    # create parent WFJT and workflow job, and link it up
    parent = WorkflowJobTemplate.objects.create(organization=organization, name='parent')
    parent_job = WorkflowJob.objects.create(workflow_job_template=parent, launch_type='workflow')
    WorkflowJobNode.objects.create(workflow_job=parent_job, unified_job_template=child, job=child_job)
    # create grandparent WFJT and workflow job and link it up
    grandparent = WorkflowJobTemplate.objects.create(organization=organization, name='grandparent')
    grandparent_job = WorkflowJob.objects.create(workflow_job_template=grandparent, launch_type='schedule')
    WorkflowJobNode.objects.create(workflow_job=grandparent_job, unified_job_template=parent, job=parent_job)
    # ancestors method gives a list of WFJT ids
    assert child_job.get_ancestor_workflows() == [parent, grandparent]


@pytest.mark.django_db
def test_workflow_ancestors_recursion_prevention(organization):
    # This is toxic database data, this tests that it doesn't create an infinite loop
    wfjt = WorkflowJobTemplate.objects.create(organization=organization, name='child')
    wfj = WorkflowJob.objects.create(workflow_job_template=wfjt, launch_type='workflow')
    WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=wfjt, job=wfj)  # well, this is a problem
    # mostly, we just care that this assertion finishes in finite time
    assert wfj.get_ancestor_workflows() == []


@pytest.mark.django_db
class TestCombinedArtifacts:
    @pytest.fixture
    def wfj_artifacts(self, job_template, organization):
        wfjt = WorkflowJobTemplate.objects.create(organization=organization, name='has_artifacts')
        wfj = WorkflowJob.objects.create(workflow_job_template=wfjt, launch_type='workflow')
        job = job_template.create_unified_job(_eager_fields=dict(artifacts={'foooo': 'bar'}, status='successful', finished=now()))
        WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=job_template, job=job)
        return wfj

    def test_multiple_types(self, project, wfj_artifacts):
        project_update = project.create_unified_job()
        WorkflowJobNode.objects.create(workflow_job=wfj_artifacts, unified_job_template=project, job=project_update)

        assert wfj_artifacts.get_effective_artifacts() == {'foooo': 'bar'}

    def test_precedence_based_on_time(self, wfj_artifacts, job_template):
        later_job = job_template.create_unified_job(
            _eager_fields=dict(artifacts={'foooo': 'zoo'}, status='successful', finished=now())  # finished later, should win
        )
        WorkflowJobNode.objects.create(workflow_job=wfj_artifacts, unified_job_template=job_template, job=later_job)

        assert wfj_artifacts.get_effective_artifacts() == {'foooo': 'zoo'}

    def test_bad_data_with_artifacts(self, organization):
        # This is toxic database data, this tests that it doesn't create an infinite loop
        wfjt = WorkflowJobTemplate.objects.create(organization=organization, name='child')
        wfj = WorkflowJob.objects.create(workflow_job_template=wfjt, launch_type='workflow')
        WorkflowJobNode.objects.create(workflow_job=wfj, unified_job_template=wfjt, job=wfj)
        job = Job.objects.create(artifacts={'foo': 'bar'}, status='successful')
        WorkflowJobNode.objects.create(workflow_job=wfj, job=job)
        # mostly, we just care that this assertion finishes in finite time
        assert wfj.get_effective_artifacts() == {'foo': 'bar'}
