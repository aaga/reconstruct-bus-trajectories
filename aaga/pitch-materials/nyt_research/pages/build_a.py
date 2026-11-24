import gfx, shell; from shell import fig
G = gfx; F = G.F; c = F["budget"]["city"]
METHODS = "https://claude.ai/artifact/HkkFNpzoEPwdLzC7mcPLrZ"
maplegend = '<div class="legend"><span><i style="border-color:var(--c-miss)"></i>Worst-delay miles outside the plan</span><span><i style="border-color:var(--c-plan)"></i>Worst-delay miles on a plan corridor</span><span><i style="border-color:var(--c-plan);opacity:.4"></i>Rest of the plan corridors</span><span><i style="border-color:var(--m-base);border-top-width:2px"></i>Other bus streets</span></div>'
body = f'''
<div class="col">
<p class="kicker">Opinion pitch · mock-up</p>
<h1>New York’s Bus Plan Skips the Street Where Buses Lose the Most Time</h1>
<p class="dek">We followed every city bus, block by block, for seven months. Half of the time they lose at rush hour is lost on 220 miles of street. The city’s new plan reaches a third of them, and Fifth Avenue is not on the list.</p>
<p class="byline">Mock-up for a pitch · Analysis of bus position reports, 27 April to 21 November 2026 · Draft text, real data</p>
<p>The average New York City bus moves at {c["mph"]:.1f} miles an hour. That number has barely changed in a decade, and everyone who rides knows what it feels like. What nobody has measured since 2017 is where the time goes.</p>
<p>So we measured it. Buses report their position about every 30 seconds. From seven months of those reports, for every route in the city, we rebuilt how long buses spend on each 10 feet of street, and how much of that is more than a clear run would take.</p>
</div>
{fig("Out of every hour on a bus, 21 minutes are spent moving", "Where the time goes on the average New York City bus, all routes, all days.", G.budget(), "Moving is the time the trip would take with green lights, no stops and no traffic. Stop + red light is a bus stop placed just before a light, where the two delays cannot be told apart. Source: analysis of MTA bus position reports, 27 April to 21 November 2026.", legend=G.legend())}
<div class="col">
<p>The rest is waiting. Twenty-one minutes of the hour go to red lights and to stops that sit beside them. Thirteen go to everything else in the way: double-parked cars, turning traffic, congestion. Only five are plain bus stops.</p>
<p>The split matters because each kind of delay has a different cure. A bus lane does nothing for a red light. Signal priority does nothing for a blocked lane.</p>
<h2>The plan, and what it leaves out</h2>
<p>In July the city and the state published a plan to speed up buses on 50 priority corridors by 20 percent. It is the most serious bus plan New York has had. We laid it over our map of delay.</p>
</div>
{fig("Half of the rush-hour delay is on these streets. Two-thirds of them are not in the plan.", "Thick lines are the 220 miles of street where buses lose the most time per mile; together they hold half of all rush-hour lost time. Blue is inside one of the plan’s 50 corridors; orange is outside. Numbers mark the twelve streets that lose the most time and are not in the plan.", G.citymap(), "Weekdays, 6 to 10am and 3 to 7pm; both directions of a street counted together. Lost time is time beyond a clear run. Corridors are from “Next Stop: Fast Buses, Better Service” (July 2026), which prints start and end points for 24 of its 50 corridors; the rest are read from its map, good to a few blocks.", scroll=False, legend=maplegend)}
<div class="col">
<p>The plan is well aimed: its corridors lose more than twice as much time per mile as the average bus street. But it is small. They cover a tenth of the bus network and a quarter of the lost time. Of the 220 worst miles, 77 are on a plan corridor and 143 are not.</p>
<p class="pull">The street that loses the most bus time in New York is Fifth Avenue: 263 bus-hours every weekday in rush hours alone, at 6.4 miles an hour.</p>
<p>Nearly a thousand buses a day run down Fifth Avenue in Midtown. From 61st Street to 34th Street, a mile and a third, they take 13 minutes. On a clear street it would take three. Most of the difference is other traffic. Fifth Avenue has two painted bus lanes on exactly this stretch, and the data says they are not keeping other traffic out. In 2020 the city announced a car-free busway here and dropped it within weeks. The redesign that replaced it would cut the two bus lanes to one. The new plan lists Madison, Sixth and Lexington Avenues, but not Fifth.</p>
</div>
{fig("Fifth Avenue, 61st to 34th Street: 13 minutes for a mile and a third", "Delay per bus along the avenue. The curve never comes down between the stops and lights; that floor is other traffic.", G.fifth(), "All routes on Fifth Avenue, all days: about 980 buses a day, 6.1 mph. Blue fading to red is a bus stop just before a light. With a light at every corner, some of the time credited to other traffic here belongs to the lights.", legend=G.legend(("near", "stop", "lights", "other")))}
<div class="col">
<p>It is not alone. Queens Boulevard, Hillside Avenue, Hylan Boulevard and Third Avenue are all among the ten streets that lose the most time, each with 130 to 165 bus-hours every weekday rush, and none is among the fifty.</p>
<h2>Twelve streets, and what each one needs</h2>
<p>Because the delay on each street is split by cause, the remedy can be read off the bar. Orange calls for a lane buses do not share, and where there is already a painted one, for something stronger than paint. Blue fading to red calls for moving the stop past the light, onto a boarding island. Red calls for lights that hold green for a bus.</p>
</div>
<figure class="wide"><p class="fig-h">The streets with the most bus delay outside the plan</p><p class="fig-s">Bars show what the delay on each street is made of. Suggested treatments follow from the mix.</p>{G.legend(("other", "near", "lights", "stop"))}{G.cards()}<figcaption>Bus-hours lost are per weekday in rush hours, both directions, beyond a clear run; a street counts as outside the plan when under a tenth of its lost time is on a plan corridor. The bars use the all-day split of each street’s delay. Eight of the twelve already have painted bus lanes or a part-time busway on some of their length; for those the suggestion is physical protection or busway hours. Existing lanes are from NYC DOT pages and news reports.</figcaption></figure>
<div class="col">
<h2>Why these three fixes</h2>
<p>We did not pick the remedies from a manual. The same seven months of data contain several real tests of them.</p>
<p><b>Keeping cars out works.</b> On 19 September the city opened a busway on East Tremont Avenue in the Bronx. Inside it, delay fell by a third westbound and by a seventh eastbound, while the rest of the Bronx did not change. Almost all of the gain was other traffic: down 56 percent westbound and 30 percent eastbound. Time at bus stops barely moved.</p>
</div>
{fig("On East Tremont Avenue, the busway took out the traffic delay and left the stops", "Delay per bus along the street before and after the busway opened on 19 September, split by what causes it.", G.tremont3(), "Above the street: weekdays 18 May to 26 June, before construction. Below it, upside down: 28 September to 20 November, with the earlier curve as a dashed line. The coloured shapes are drawn smoothly and are indicative. Orange is other traffic, blue is bus stops, blue fading to red is a stop just before a light, red is lights. The busway runs eastbound from Third Avenue to Southern Boulevard and westbound from Southern Boulevard to Belmont Avenue, 6am to 8pm.", legend=G.legend(("other", "near", "stop", "lights")))}
<div class="col">
<p><b>Where the stop sits matters.</b> Set traffic aside and a light with no bus stop costs a bus 11 seconds. Put a stop just after the light and the total is 23 seconds. Put it just before, as New York does at more than 3,300 lights, and the bus waits to load, then waits again for the green: 31 seconds.</p>
</div>
{fig("A stop before the light costs more than a stop after it", "Average delay around an intersection, for buses going straight through. The light is at the centre of each panel.", G.placement3(), "Only the delay credited to the light and the stop is shown; other traffic is removed. Averages over intersections with at least 350 feet of clear street before and 300 after, buses going straight through. Seconds are per 100 feet. With a far-side stop, delay before the light is counted as the light’s and delay after it as the stop’s. Stops are not placed at random, so part of the gap may reflect busier corners. Counting all time above a clear run within 250 feet, traffic included, gives the same order: 30, 39 and 43 seconds.", legend=G.legend(("lights", "stop", "near")))}
<div class="col">
<p><b>Signal priority helps, a little, and only at the right lights.</b> The city switched on priority at more than 280 intersections during these months. At lights with no stop in front of them, buses saved about 0.7 seconds each, or 4 percent. At lights with a stop just before them, nothing changed: a bus that is loading cannot use a green. A few intersections did far better, with a fifth of the wait gone, which suggests the settings matter as much as the hardware.</p>
<h2>What we are asking for</h2>
<ul>
<li><b>Add the missing streets.</b> Start with Fifth Avenue, and put Queens Boulevard, Hillside Avenue, Hylan Boulevard and Third Avenue on the list.</li>
<li><b>Match the fix to the delay.</b> Publish, for every corridor, how much of the delay is traffic, lights and stops, and choose the treatment from that.</li>
<li><b>Move stops across the intersection</b> wherever a boarding island fits. It is the cheapest item here and makes signal priority work.</li>
</ul>
<div class="box"><h3>How we did this</h3>
<p>Every MTA bus reports its position about every 30 seconds. We rebuilt each trip’s path along its route, for 209 days and about 31 billion 10-foot steps, and averaged the time spent on each step. Subtracting the time a bus takes late at night with nothing in the way leaves the delay. Peaks in that delay that sit at a bus stop or a traffic light are credited to it; the smooth level underneath the peaks is other traffic.</p>
<p>Our citywide speed, 8.3 mph, matches the MTA’s own figure for 2026. The full method, with its limits, is in <a href="{METHODS}">Where NYC Buses Lose Time</a>.</p>
<p><b>Limits.</b> Positions every 30 seconds blur short stops. There is no door data, so stop delay includes pulling in and out. On streets with a light every block, some light and stop delay is counted as other traffic. Bus-hours count buses, not riders. Twenty-six of the plan’s corridors have no printed limits and were read from its map.</p></div>
</div>
'''
open(shell.HERE / "pitch_a.html", "w").write(shell.page("The Streets the Bus Plan Missed", body)); print(len(body) // 1024, "KB")
