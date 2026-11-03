import asyncio
from benchmarks.core_evaluation import progressive_infer
from weyra.contracts import ModelInfo, ModelResponse, Usage, ToolCall
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget


def test_progressive_real_skill_tool_load(tmp_path):
    registry=ProviderRegistry()
    registry.register('test',ScriptedProvider([
       ModelResponse('',tool_calls=(ToolCall('c','skill',{'name':'test-skill'}),),usage=Usage(10,1)),
       ModelResponse('{"x":1}',usage=Usage(10,1))]))
    runtime=ModelRuntime(registry,{'main':ModelInfo('test','m',20000,1000)})
    case={'id':'t','input':{'objective':'Apply test-skill','output_contract':{'x':'int'}},'grading':{'expected':{'x':1}}}
    result=asyncio.run(progressive_infer(case,'Return x=1','test-skill',tmp_path,runtime,Budget()))
    assert result['status']=='pass'
    assert result['skill_loaded'] is True
