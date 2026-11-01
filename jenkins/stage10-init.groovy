import jenkins.model.Jenkins
import hudson.model.Node
import hudson.security.FullControlOnceLoggedInAuthorizationStrategy
import hudson.tasks.LogRotator
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey
import groovy.json.JsonOutput

def j = Jenkins.get()
j.setNumExecutors(1)
j.setMode(Node.Mode.NORMAL)
j.setSlaveAgentPort(-1)
def strategy = new FullControlOnceLoggedInAuthorizationStrategy()
strategy.setAllowAnonymousRead(false)
j.setAuthorizationStrategy(strategy)
j.save()

def provider = SystemCredentialsProvider.getInstance()
def store = provider.getStore()
def credentialId = 'webops-stage10-deploy'
def credentials = store.getCredentials(Domain.global())
if (!credentials.any { it.id == credentialId }) {
  def key = new File('/var/lib/webops-stage10/bootstrap-secrets/deploy-key')
  if (!key.isFile()) throw new IllegalStateException('STAGE10_DEDICATED_KEY_MISSING')
  def credential = new BasicSSHUserPrivateKey(CredentialsScope.GLOBAL, credentialId,
    'webops-deploy', new BasicSSHUserPrivateKey.DirectEntryPrivateKeySource(key.getText('UTF-8')),
    null, 'Stage10 dedicated identity; no target authorization or deployment binding yet')
  if (!store.addCredentials(Domain.global(), credential)) {
    throw new IllegalStateException('STAGE10_CREDENTIAL_STORE_WRITE_FAILED')
  }
  provider.save()
}

def name = 'stage10-verify'
def job = j.getItem(name)
if (job == null) job = j.createProject(WorkflowJob, name)
if (!(job instanceof WorkflowJob)) throw new IllegalStateException('STAGE10_JOB_TYPE_MISMATCH')
job.setDescription('Trusted manual stage10 verification only. Fixed SHA; no webhook, polling, PR or deployment.')
job.setDefinition(new CpsFlowDefinition(new File('/var/lib/webops-stage10/stage10-job.groovy').getText('UTF-8'), true))
job.setBuildDiscarder(new LogRotator(7, 5, 1, 1))
if (job.getProperty(DisableConcurrentBuildsJobProperty) == null) {
  job.addProperty(new DisableConcurrentBuildsJobProperty())
}
job.getTriggers().values().each { job.removeTrigger(it.descriptor) }
job.save()

def report = [executors: j.numExecutors, agentPort: j.slaveAgentPort,
  anonymousRead: false, credentialId: credentialId,
  credentialsPresent: store.getCredentials(Domain.global()).any { it.id == credentialId },
  credentialTargetAuthorized: false, job: name, jobSandbox: true,
  fixedSha: '48c4ee51a408e574a83e08e900680641821060bc',
  plugins: j.pluginManager.plugins.collectEntries { [(it.shortName): [version: it.version, active: it.active]] }]
new File('/var/lib/webops-stage10/jenkins-status.json').setText(JsonOutput.prettyPrint(JsonOutput.toJson(report)) + '\n', 'UTF-8')
println('STAGE10_BOOTSTRAP_CONFIG_AND_CREDENTIAL_STORE_OK')
