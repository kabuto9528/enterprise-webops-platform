/* Trusted main only. SCM checkout records the polling baseline; root holds keys. */
node {
  try {
    timeout(time: 100, unit: 'MINUTES') {
      deleteDir()
      def revision = checkout(
        scm: [$class: 'GitSCM', branches: [[name: '*/main']],
              userRemoteConfigs: [[url: 'http://127.0.0.1:19418/webops.git']],
              extensions: [[$class: 'CloneOption', noTags: true, shallow: false, timeout: 2]]],
        poll: true, changelog: true)
      withEnv(["TARGET_SHA=${revision.GIT_COMMIT}"]) {
        sh 'bash scripts/ci-pipeline.sh'
      }
    }
  } catch (error) {
    currentBuild.result = 'FAILURE'
    throw error
  } finally {
    try {
      if (!fileExists('release-evidence.txt')) {
        writeFile file: 'release-evidence.txt',
          text: "job=${env.JOB_NAME}\nbuild=${env.BUILD_NUMBER}\nresult=${currentBuild.currentResult}\nevidence=incomplete\n"
      }
      archiveArtifacts artifacts: 'release-evidence.txt, build/*.tar, build/*.json, build/*.sha256', allowEmptyArchive: true
    } finally {
      deleteDir()
    }
  }
}
