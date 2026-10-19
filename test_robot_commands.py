import unittest

from robot_commands import (
    RobotCommandQueue,
    RobotCommandError,
    command_from_dict,
    parse_robot_command,
)


class RobotCommandTests(unittest.TestCase):
    def test_parse_english(self):
        self.assertEqual(parse_robot_command("go forward").action, "forward")
        self.assertEqual(parse_robot_command("move forward for 2 seconds").duration_ms, 2000)
        self.assertEqual(parse_robot_command("turn left").action, "left")
        self.assertEqual(parse_robot_command("stop the robot").action, "stop")

    def test_parse_vietnamese(self):
        command = parse_robot_command("đi thẳng 2 giây")
        self.assertEqual((command.action, command.duration_ms), ("forward", 2000))

    def test_validation(self):
        with self.assertRaises(RobotCommandError):
            command_from_dict({"action": "fly", "speed": 1, "duration_ms": 100})
        with self.assertRaises(RobotCommandError):
            command_from_dict({"action": "left", "speed": 256, "duration_ms": 100})
        with self.assertRaises(RobotCommandError):
            command_from_dict({"action": "left", "speed": 1, "duration_ms": 10001})

    def test_queue_consumes_once(self):
        queue = RobotCommandQueue()
        command = parse_robot_command("turn right")
        queue.put(command)
        self.assertIs(queue.get(), command)
        self.assertIsNone(queue.get())


if __name__ == "__main__":
    unittest.main()
