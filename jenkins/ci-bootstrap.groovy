/*
 * Stage 11 Jenkins bootstrap (root-provisioned, executed from init.groovy.d).
 *
 * Creates or updates the single CI/CD job `webops-ci`:
 *   - Pipeline script from SCM: /srv/git/webops.git, branch main, Jenkinsfile
 *   - lightweight checkout, one concurrent build, 14 days / 10 builds retained
 *   - SCM polling only when /etc/webops/ci/polling-enabled exists (phase 2)
 *   - the very first build is scheduled once by the operator-provisioned script,
 *     so the pipeline is proven before polling is switched on
 *
 * No webhook, no pull-request trigger, no free-form SHA parameter, no extra
 * plugin and no credential are added here.
 */

import hudson.plugins.git.BranchSpec
import hudson.plugins.git.GitSCM
import hudson.plugins.git.UserRemoteConfig
import hudson.tasks.LogRotator
import hudson.triggers.SCMTrigger
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty

def JOB_NAME = 'webops-ci'
def BARE = 'http://127.0.0.1:19418/webops.git'
def POLLING_FLAG = new File('/etc/webops/ci/polling-enabled')
def BUILD_REQUEST = new File('/var/lib/webops-ci/request-build')

try {
  def jenkins = Jenkins.get()
  def polling = POLLING_FLAG.isFile()

  // git plugin 5.11.0: the only full constructor is the 5-argument
  // @DataBoundConstructor (configs, branches, browser, gitTool, extensions).
  def scm = new GitSCM(
    [new UserRemoteConfig(BARE, 'origin', null, null)],
    [new BranchSpec('*/main')],
    null, null, [])

  def job = jenkins.getItem(JOB_NAME)
  if (job == null) {
    job = jenkins.createProject(WorkflowJob, JOB_NAME)
    println("STAGE11_JOB_CREATED=${JOB_NAME}")
  }
  if (!(job instanceof WorkflowJob)) {
    throw new IllegalStateException('STAGE11_JOB_TYPE_MISMATCH')
  }

  def definition = new CpsScmFlowDefinition(scm, 'Jenkinsfile')
  definition.setLightweight(true)
  job.setDefinition(definition)
  job.setDescription('Stage11 CI/CD: SCM polling -> checkout -> test -> package -> root deploy unit -> verify. No webhook, no PR trigger, no free-form SHA.')
  job.setBuildDiscarder(new LogRotator(14, 10, 5, 5))
  if (job.getProperty(DisableConcurrentBuildsJobProperty) == null) {
    job.addProperty(new DisableConcurrentBuildsJobProperty())
  }
  job.setTriggers(polling ? [new SCMTrigger('H/2 * * * *')] : [])
  job.save()

  println("STAGE11_JOB_SAVED=${JOB_NAME} polling=${polling} triggers=${job.getTriggers().size()}")

  // Operator-initiated build: the provisioning script drops request-build before
  // restarting Jenkins and removes it once the login page answers. It works with
  // or without polling, so one explicit build can be requested at any time.
  if (BUILD_REQUEST.isFile()) {
    job.scheduleBuild2(0)
    println('STAGE11_BUILD_SCHEDULED=1')
  } else {
    println('STAGE11_BUILD_SCHEDULED=0')
  }
} catch (Throwable error) {
  // Never let a provisioning problem stop Jenkins from starting.
  println("STAGE11_BOOTSTRAP_ERROR=${error}")
  error.printStackTrace(System.out)
}
