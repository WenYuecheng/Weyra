"""Small terminal event renderer, independent of the execution loop."""
import sys


class StreamDisplay:
    def __init__(self, output=None):
        self.output = output if output is not None else sys.stdout
        self.current = ''

    def event(self, kind, data):
        if kind == 'text':
            self.current += str(data)
            print(data, end='', file=self.output, flush=True)
        elif kind == 'attempt_reset':
            if self.current:
                print('\n[本次尝试未完成，以上为临时输出]', file=self.output, flush=True)
            self.current = ''
        elif kind == 'tool_completed':
            print(f"\nTool {data.get('call_id', '')}: {data.get('status', '')}",file=self.output,flush=True)
            self.current = ''

    def finish(self, answer):
        if self.current == answer:
            print(file=self.output, flush=True)
        else:
            print(answer, file=self.output, flush=True)
        self.current = ''
