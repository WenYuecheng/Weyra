import asyncio
from weyra.jobs import Jobs


def test_job(tmp_path):
    async def run():
        jobs = Jobs(tmp_path)
        job = await jobs.start("printf hello", "turn")
        done = await jobs.wait(job["job_id"], 2)
        assert done["returncode"] == 0
        assert open(done["output_path"]).read() == "hello"
        await jobs.close()

    asyncio.run(run())


def test_wait_timeout_and_owner(tmp_path):
    async def run():
        jobs = Jobs(tmp_path)
        job = await jobs.start("sleep 20", "turn")
        assert (await jobs.wait(job["job_id"], 0.01))["running"]
        jobs.transfer(job["job_id"], "turn", "next")
        await jobs.cleanup_owner("turn")
        assert jobs.status(job["job_id"])["running"]
        await jobs.cleanup_owner("next")
        assert not jobs.status(job["job_id"])["running"]

    asyncio.run(run())
