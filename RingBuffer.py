from collections import deque


class RingBuffer(deque):
    def __init__(self, size):
        super().__init__()
        self.size = int(size)

    def append(self, item):
        super().append(item)
        while len(self) > self.size:
            self.popleft()

    def get(self):
        return list(self)


if __name__ == "__main__":
    ring = RingBuffer(5)
    for value in range(9):
        ring.append(value)
        print(ring.get())
