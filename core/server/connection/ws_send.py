from core.tools.asyncio_to_thread import to_thread
from .. import logger


async def ws_send(app):
    state = app.state
    logger.info('WebSocket 发送任务已启动')
    while True:
        try:
            result = await to_thread(state.queue_out.get)
            if result is None:
                logger.info('收到退出通知，停止发送任务')
                return
            connection = state.recognition_connections.get(result.socket_id)
            if connection is not None:
                await connection.publish(result)
        except Exception as error:
            logger.error('发送结果时发生错误: %s', error, exc_info=True)
