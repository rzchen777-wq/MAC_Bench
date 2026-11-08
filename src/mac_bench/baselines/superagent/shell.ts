/** DSH bash behavior inside the benchmark's isolated task world. */
import { LocalBashExecutor } from '@deepseek-ai/dsh-bash-local'
import type { ShellExecRequest, ShellExecSpec, ShellExecution } from '@deepseek-ai/dsh-shell'

export default class TaskShell extends LocalBashExecutor {
  get sandboxMode() { return 'danger-full-access' as const }

  resolve(request: ShellExecRequest): ShellExecSpec {
    return this.check(super.resolve(request))
  }

  execute(spec: ShellExecSpec): Promise<ShellExecution> {
    return super.execute(this.check(spec))
  }

  private check(spec: ShellExecSpec): ShellExecSpec {
    // Access is full inside the supplied task sandbox, never the runner host.
    // This provider does not claim to implement additional read-only presets.
    if (spec.sandboxPolicy && spec.sandboxPolicy.mode !== 'danger-full-access') {
      throw new Error('Task commands use the benchmark sandbox permission policy')
    }
    return spec
  }
}
