export class LatestRequestGate {
  private generation = 0;

  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  invalidate(requestId: number): void {
    if (this.isCurrent(requestId)) this.generation += 1;
  }

  isCurrent(requestId: number): boolean {
    return requestId === this.generation;
  }
}
