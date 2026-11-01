/* Compile-only check against the installed core/plugin jars. Never installed
 * into init.groovy.d; the runtime bootstrap contains no script helper methods.
 */
import groovy.transform.CompileStatic
import hudson.triggers.SCMTrigger
import hudson.triggers.Trigger
import org.jenkinsci.plugins.workflow.job.WorkflowJob

@CompileStatic
class Stage11ApiCheck {
  static void verifyTriggerApi(WorkflowJob job, boolean enabled) {
    List<Trigger<?>> triggers = new ArrayList<>()
    if (enabled) {
      triggers.add(new SCMTrigger('H/2 * * * *'))
    }
    job.setTriggers(triggers)
  }
}
